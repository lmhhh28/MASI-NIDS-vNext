package event

import (
	"context"
	"errors"
	"fmt"
	"time"
)

// SweepRetention removes at most limit expired canonical Event rows and then
// unreferenced global identity rows in one short transaction. Incident and
// projection references retain their identity registry row even after the full
// Event payload reaches its configured retention boundary.
func (s *IngestService) SweepRetention(ctx context.Context, now time.Time, retention time.Duration, limit int) (eventsDeleted, identitiesDeleted int64, err error) {
	if s == nil || s.pool == nil || retention < 24*time.Hour || retention > 365*24*time.Hour || limit < 1 || limit > 1000 {
		return 0, 0, errors.New("event: retention arguments invalid")
	}
	err = s.pool.Pool.QueryRow(ctx, `SELECT events_deleted,identities_deleted
	 FROM masi_sweep_event_retention($1,$2)`, now.Add(-retention), limit).
		Scan(&eventsDeleted, &identitiesDeleted)
	if err != nil {
		err = fmt.Errorf("event: bounded retention function: %w", err)
	}
	return eventsDeleted, identitiesDeleted, err
}
