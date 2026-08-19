package api

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// OIDCClient is a minimal, dependency-free authorization-code + PKCE client.
// Go owns the OIDC callback/session (the SPA never sees tokens). ID tokens
// are verified as RS256 JWTs against the provider JWKS with iss/aud/exp/nonce
// checks; plaintext/none algorithms are rejected (no plaintext fallback).
type OIDCClient struct {
	Issuer      string
	ClientID    string
	RedirectURI string
	// Scopes requested (must include openid).
	Scopes []string
	// ClientSecret is optional (confidential client); public PKCE flow when
	// empty. It is injected from a secret file, never persisted.
	ClientSecret string

	hc                    *http.Client
	now                   func() time.Time
	keys                  map[string]*rsa.PublicKey
	keysAt                time.Time
	keysMu                sync.RWMutex
	refreshMu             sync.Mutex
	authorizationEndpoint string
	tokenEndpoint         string
	jwksURI               string
}

// NewOIDCClient builds the client. Initialize must fetch and validate discovery
// plus JWKS before the router is exposed.
func NewOIDCClient(issuer, clientID, redirectURI, clientSecret string) *OIDCClient {
	return &OIDCClient{
		Issuer: strings.TrimSuffix(issuer, "/"), ClientID: clientID,
		RedirectURI: redirectURI, ClientSecret: clientSecret,
		Scopes: []string{"openid", "profile"},
		hc: &http.Client{
			Timeout: 10 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns: 4, IdleConnTimeout: 60 * time.Second,
				ResponseHeaderTimeout: 10 * time.Second,
			},
		},
		now:  time.Now,
		keys: map[string]*rsa.PublicKey{},
	}
}

// AuthCodeURL builds the authorization redirect with S256 PKCE. state is
// HMAC-bound to the nonce and checked at callback.
func (c *OIDCClient) AuthCodeURL(state, nonce string) (string, string, error) {
	if c.authorizationEndpoint == "" {
		return "", "", errors.New("api: OIDC client not initialized from discovery")
	}
	verifier, err := randomB64(64)
	if err != nil {
		return "", "", err
	}
	sum := sha256.Sum256([]byte(verifier))
	challenge := base64.RawURLEncoding.EncodeToString(sum[:])
	q := url.Values{
		"response_type":         {"code"},
		"client_id":             {c.ClientID},
		"redirect_uri":          {c.RedirectURI},
		"scope":                 {strings.Join(c.Scopes, " ")},
		"state":                 {state},
		"nonce":                 {nonce},
		"code_challenge":        {challenge},
		"code_challenge_method": {"S256"},
	}
	return c.authorizationEndpoint + "?" + q.Encode(), verifier, nil
}

type tokenResponse struct {
	AccessToken string `json:"access_token"`
	IDToken     string `json:"id_token"`
	TokenType   string `json:"token_type"`
	ExpiresIn   int    `json:"expires_in"`
}

// Exchange performs the token request (bounded body) and returns the raw ID
// token for verification.
func (c *OIDCClient) Exchange(ctx context.Context, code, verifier string) (string, error) {
	form := url.Values{
		"grant_type":    {"authorization_code"},
		"code":          {code},
		"redirect_uri":  {c.RedirectURI},
		"client_id":     {c.ClientID},
		"code_verifier": {verifier},
	}
	if c.ClientSecret != "" {
		form.Set("client_secret", c.ClientSecret)
	}
	if c.tokenEndpoint == "" {
		return "", errors.New("api: OIDC token endpoint unavailable")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.tokenEndpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := c.hc.Do(req)
	if err != nil {
		return "", fmt.Errorf("api: oidc token endpoint: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("api: oidc token status %d", resp.StatusCode)
	}
	body, err := readOIDCBody(resp.Body)
	if err != nil {
		return "", err
	}
	var tr tokenResponse
	if err := json.Unmarshal(body, &tr); err != nil {
		return "", fmt.Errorf("api: oidc token body: %w", err)
	}
	if tr.IDToken == "" {
		return "", errors.New("api: oidc response missing id_token")
	}
	return tr.IDToken, nil
}

type discoveryDoc struct {
	Issuer                string `json:"issuer"`
	JWKSURI               string `json:"jwks_uri"`
	AuthorizationEndpoint string `json:"authorization_endpoint"`
	TokenEndpoint         string `json:"token_endpoint"`
}

func (c *OIDCClient) discovery(ctx context.Context) (*discoveryDoc, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.Issuer+"/.well-known/openid-configuration", nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, fmt.Errorf("api: oidc discovery: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("api: oidc discovery status %d", resp.StatusCode)
	}
	var d discoveryDoc
	body, err := readOIDCBody(resp.Body)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, err
	}
	if d.Issuer != c.Issuer {
		return nil, fmt.Errorf("api: oidc discovery issuer mismatch (%s != %s)", d.Issuer, c.Issuer)
	}
	for _, endpoint := range []string{d.AuthorizationEndpoint, d.TokenEndpoint, d.JWKSURI} {
		u, err := url.Parse(endpoint)
		if err != nil || u.Scheme != "https" || u.Host == "" {
			return nil, errors.New("api: OIDC discovery endpoint must be absolute HTTPS")
		}
	}
	return &d, nil
}

func (c *OIDCClient) Initialize(ctx context.Context) error {
	d, err := c.discovery(ctx)
	if err != nil {
		return err
	}
	c.authorizationEndpoint = d.AuthorizationEndpoint
	c.tokenEndpoint = d.TokenEndpoint
	c.jwksURI = d.JWKSURI
	return c.jwks(ctx, d.JWKSURI)
}

type jwksDoc struct {
	Keys []struct {
		Kty string `json:"kty"`
		Kid string `json:"kid"`
		N   string `json:"n"`
		E   string `json:"e"`
	} `json:"keys"`
}

func (c *OIDCClient) jwks(ctx context.Context, uri string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, uri, nil)
	if err != nil {
		return err
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return fmt.Errorf("api: oidc jwks: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("api: oidc jwks status %d", resp.StatusCode)
	}
	var j jwksDoc
	body, err := readOIDCBody(resp.Body)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(body, &j); err != nil {
		return err
	}
	keys := map[string]*rsa.PublicKey{}
	for _, k := range j.Keys {
		if k.Kty != "RSA" || k.Kid == "" {
			continue
		}
		nb, err := base64.RawURLEncoding.DecodeString(k.N)
		if err != nil {
			continue
		}
		eb, err := base64.RawURLEncoding.DecodeString(k.E)
		if err != nil {
			continue
		}
		if len(eb) == 0 || len(eb) > 4 {
			continue
		}
		e := 0
		for _, b := range eb {
			e = e<<8 | int(b)
		}
		n := new(big.Int).SetBytes(nb)
		if n.BitLen() < 2048 || e < 3 || e%2 == 0 {
			continue
		}
		keys[k.Kid] = &rsa.PublicKey{N: n, E: e}
	}
	if len(keys) == 0 {
		return errors.New("api: OIDC JWKS has no acceptable RSA-2048 RS256 key")
	}
	c.keysMu.Lock()
	c.keys = keys
	c.keysAt = c.now()
	c.keysMu.Unlock()
	return nil
}

func (c *OIDCClient) key(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	if k, ok := c.cachedKey(kid); ok {
		return k, nil
	}
	if c.jwksURI == "" {
		return nil, errors.New("api: OIDC client not initialized")
	}
	c.refreshMu.Lock()
	defer c.refreshMu.Unlock()
	// Another request may have completed the refresh while this request waited.
	if k, ok := c.cachedKey(kid); ok {
		return k, nil
	}
	if err := c.jwks(ctx, c.jwksURI); err != nil {
		return nil, err
	}
	if k, ok := c.cachedKey(kid); ok {
		return k, nil
	}
	return nil, fmt.Errorf("api: oidc unknown kid %q", kid)
}

func (c *OIDCClient) cachedKey(kid string) (*rsa.PublicKey, bool) {
	c.keysMu.RLock()
	defer c.keysMu.RUnlock()
	k, ok := c.keys[kid]
	return k, ok && c.now().Sub(c.keysAt) < time.Hour
}

// VerifyIDToken verifies the RS256 ID token and returns the accepted claims.
// alg=none/HS256, issuer/audience mismatch, expiry, or nonce mismatch are all
// rejected (fail closed).
func (c *OIDCClient) VerifyIDToken(ctx context.Context, idToken, wantNonce string) (*actorClaims, error) {
	parts := strings.Split(idToken, ".")
	if len(parts) != 3 {
		return nil, errors.New("api: id_token not a JWS compact serialization")
	}
	var hdr struct {
		Alg string `json:"alg"`
		Kid string `json:"kid"`
	}
	hb, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return nil, fmt.Errorf("api: id_token header: %w", err)
	}
	if err := json.Unmarshal(hb, &hdr); err != nil {
		return nil, fmt.Errorf("api: id_token header json: %w", err)
	}
	if hdr.Alg != "RS256" {
		return nil, fmt.Errorf("api: id_token alg %q rejected (only RS256)", hdr.Alg)
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return nil, fmt.Errorf("api: id_token signature encoding: %w", err)
	}
	pub, err := c.key(ctx, hdr.Kid)
	if err != nil {
		return nil, err
	}
	signed := parts[0] + "." + parts[1]
	sum := sha256.Sum256([]byte(signed))
	if err := rsa.VerifyPKCS1v15(pub, crypto.SHA256, sum[:], sig); err != nil {
		return nil, fmt.Errorf("api: id_token signature invalid: %w", err)
	}
	pb, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("api: id_token payload: %w", err)
	}
	var cl actorClaims
	if err := json.Unmarshal(pb, &cl); err != nil {
		return nil, fmt.Errorf("api: id_token claims: %w", err)
	}
	if cl.Issuer != c.Issuer {
		return nil, fmt.Errorf("api: id_token iss %q != %q", cl.Issuer, c.Issuer)
	}
	audienceMatch := false
	for _, aud := range cl.Audience {
		if aud == c.ClientID {
			audienceMatch = true
			break
		}
	}
	if !audienceMatch {
		return nil, fmt.Errorf("api: id_token aud mismatch")
	}
	if len(cl.Audience) > 1 && cl.AuthorizedParty != c.ClientID {
		return nil, errors.New("api: multi-audience token requires matching azp")
	}
	if cl.Subject == "" {
		return nil, errors.New("api: id_token sub missing")
	}
	now := c.now().Unix()
	if cl.ExpiresAt <= now {
		return nil, errors.New("api: id_token expired")
	}
	if cl.IssuedAt <= 0 || cl.IssuedAt > now+300 || cl.IssuedAt < now-600 {
		return nil, errors.New("api: id_token issued-at outside accepted login window")
	}
	if cl.NotBefore > now+30 {
		return nil, errors.New("api: id_token not active")
	}
	if cl.AuthorizedParty != "" && cl.AuthorizedParty != c.ClientID {
		return nil, errors.New("api: id_token authorized-party mismatch")
	}
	if wantNonce != "" && cl.Nonce != wantNonce {
		return nil, errors.New("api: id_token nonce mismatch")
	}
	return &cl, nil
}

// randomB64 returns n random bytes url-safe base64 encoded.
func randomB64(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// randomState mints a state value with an embedded expiry (10 min).
func randomState() (string, error) {
	exp := make([]byte, 8)
	binary.BigEndian.PutUint64(exp, uint64(time.Now().Add(10*time.Minute).Unix()))
	r, err := randomB64(24)
	if err != nil {
		return "", err
	}
	return r + "." + base64.RawURLEncoding.EncodeToString(exp), nil
}

func stateValid(state string) bool {
	parts := strings.SplitN(state, ".", 2)
	if len(parts) != 2 {
		return false
	}
	exp, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil || len(exp) != 8 {
		return false
	}
	return int64(binary.BigEndian.Uint64(exp)) > time.Now().Unix()
}

const maxOIDCResponseBytes = 1 << 20

func readOIDCBody(r io.Reader) ([]byte, error) {
	body, err := io.ReadAll(io.LimitReader(r, maxOIDCResponseBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxOIDCResponseBytes {
		return nil, errors.New("api: OIDC response exceeds 1 MiB")
	}
	return body, nil
}
