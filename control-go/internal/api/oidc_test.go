package api

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestOIDCDiscoveryPKCEExchangeAndConcurrentVerify(t *testing.T) {
	privateKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1_800_000_000, 0)
	var issuer, signedToken string
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			_ = json.NewEncoder(w).Encode(map[string]string{
				"issuer": issuer, "authorization_endpoint": issuer + "/authorize",
				"token_endpoint": issuer + "/token", "jwks_uri": issuer + "/jwks",
			})
		case "/jwks":
			_ = json.NewEncoder(w).Encode(map[string]any{"keys": []map[string]string{{
				"kty": "RSA", "kid": "key-1",
				"n": base64.RawURLEncoding.EncodeToString(privateKey.N.Bytes()),
				"e": base64.RawURLEncoding.EncodeToString([]byte{1, 0, 1}),
			}}})
		case "/token":
			if err := r.ParseForm(); err != nil || r.Form.Get("code_verifier") != "verifier" || r.Form.Get("code") != "code" {
				http.Error(w, "bad token request", http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"id_token": signedToken, "token_type": "Bearer", "expires_in": 300})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	issuer = server.URL

	client := NewOIDCClient(issuer, "control-client", issuer+"/callback", "")
	client.hc = server.Client()
	client.now = func() time.Time { return now }
	if err := client.Initialize(context.Background()); err != nil {
		t.Fatal(err)
	}
	authURL, verifier, err := client.AuthCodeURL("state", "nonce")
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := url.Parse(authURL)
	if err != nil || parsed.Path != "/authorize" || parsed.Query().Get("code_challenge_method") != "S256" || verifier == "" {
		t.Fatalf("bad authorization URL/verifier: %s %q err=%v", authURL, verifier, err)
	}
	signedToken = signOIDCTestToken(t, privateKey, map[string]any{
		"iss": issuer, "sub": "operator-1", "aud": "control-client", "exp": now.Unix() + 300,
		"iat": now.Unix(), "nbf": now.Unix() - 1, "nonce": "nonce", "amr": []string{"webauthn"},
		"auth_time": now.Unix(), "acr": "urn:masi:acr:phishing-resistant",
	})
	gotToken, err := client.Exchange(context.Background(), "code", "verifier")
	if err != nil || gotToken != signedToken {
		t.Fatalf("exchange token mismatch err=%v", err)
	}

	var wg sync.WaitGroup
	errs := make(chan error, 32)
	for i := 0; i < cap(errs); i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			claims, err := client.VerifyIDToken(context.Background(), signedToken, "nonce")
			if err != nil {
				errs <- err
				return
			}
			if claims.Subject != "operator-1" {
				errs <- fmt.Errorf("subject=%q", claims.Subject)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
}

func TestOIDCResponseHardLimit(t *testing.T) {
	_, err := readOIDCBody(strings.NewReader(strings.Repeat("x", maxOIDCResponseBytes+1)))
	if err == nil || !strings.Contains(err.Error(), "exceeds") {
		t.Fatalf("oversize OIDC response must fail, got %v", err)
	}
}

func signOIDCTestToken(t *testing.T, key *rsa.PrivateKey, claims map[string]any) string {
	t.Helper()
	header := oidcTestJSON(t, map[string]string{"alg": "RS256", "kid": "key-1", "typ": "JWT"})
	payload := oidcTestJSON(t, claims)
	signed := base64.RawURLEncoding.EncodeToString(header) + "." + base64.RawURLEncoding.EncodeToString(payload)
	digest := sha256.Sum256([]byte(signed))
	signature, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return signed + "." + base64.RawURLEncoding.EncodeToString(signature)
}

func oidcTestJSON(t *testing.T, value any) []byte {
	t.Helper()
	var out bytes.Buffer
	if err := json.NewEncoder(&out).Encode(value); err != nil {
		t.Fatal(err)
	}
	return bytes.TrimSpace(out.Bytes())
}
