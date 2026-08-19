// Package logging provides structured, bounded, desensitized logging for Go
// Control Core. Raw ID tokens, access tokens, cookies, session secrets, and
// sensitive OIDC claims are never logged (SEC-002). Log volume is bounded by
// the resource profile; trace/metrics are not a source of truth.
package logging

import (
	"log/slog"
	"os"
	"regexp"
	"strings"
)

// Redactor scrubs sensitive values from log records before emission.
type Redactor struct {
	patterns []*regexp.Regexp
}

// NewRedactor builds a redactor that masks bearer tokens, cookies, OIDC
// tokens, and key=value secret assignments.
func NewRedactor() *Redactor {
	return &Redactor{
		patterns: []*regexp.Regexp{
			regexp.MustCompile(`(?i)(bearer\s+)[A-Za-z0-9._-]+`),
			regexp.MustCompile(`(?i)(cookie:\s*)[^\s]+`),
			regexp.MustCompile(`(?i)(access_token|id_token|refresh_token|session|secret|password|api_key)(\s*[=:]\s*)[^\s&]+`),
		},
	}
}

// Redact returns the input with sensitive substrings replaced by [REDACTED].
func (r *Redactor) Redact(s string) string {
	for _, p := range r.patterns {
		s = p.ReplaceAllString(s, "${1}[REDACTED]")
	}
	return s
}

// New constructs the leveled structured logger. Level is INFO by default;
// DEBUG requires MASI_CTRL_LOG_DEBUG=1 (never enabled in production profiles).
func New() *slog.Logger {
	level := slog.LevelInfo
	if os.Getenv("MASI_CTRL_LOG_DEBUG") == "1" {
		level = slog.LevelDebug
	}
	redactor := NewRedactor()
	handler := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level:     level,
		AddSource: true,
		ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
			if a.Value.Kind() == slog.KindString {
				return slog.String(a.Key, redactor.Redact(a.Value.String()))
			}
			return a
		},
	})
	return slog.New(handler)
}

// IsSensitive reports whether a field name is sensitive and must not be logged
// verbatim. Used by callers that construct attributes manually.
func IsSensitive(name string) bool {
	n := strings.ToLower(name)
	switch n {
	case "access_token", "id_token", "refresh_token", "session", "secret",
		"password", "passwd", "api_key", "cookie", "authorization", "sse_hmac_key":
		return true
	}
	return false
}
