package firewall

import "encoding/json"

// marshalJSON is a thin wrapper to centralize JSON marshalling for the package.
func marshalJSON(v any) ([]byte, error) {
	return json.Marshal(v)
}
