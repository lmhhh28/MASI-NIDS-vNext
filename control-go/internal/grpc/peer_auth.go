package grpcapi

import (
	"context"
	"errors"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"

	"masi-nids/control-go/internal/db"
)

type peerWorkloadKey struct{}

// UnaryPeerIdentityInterceptor maps one exact allowlisted client DNS SAN to the
// authenticated Edge workload. A CA-valid certificate with no/ambiguous
// workload SAN is rejected before the application handler sees its body.
func UnaryPeerIdentityInterceptor(allowedSANs []string) grpc.UnaryServerInterceptor {
	allowed := make(map[string]struct{}, len(allowedSANs))
	for _, san := range allowedSANs {
		allowed[san] = struct{}{}
	}
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		_ = info
		p, ok := peer.FromContext(ctx)
		if !ok {
			return nil, status.Error(codes.Unauthenticated, "mTLS peer unavailable")
		}
		tlsInfo, ok := p.AuthInfo.(credentials.TLSInfo)
		if !ok || len(tlsInfo.State.VerifiedChains) == 0 || len(tlsInfo.State.PeerCertificates) == 0 {
			return nil, status.Error(codes.Unauthenticated, "verified mTLS peer required")
		}
		identity := ""
		for _, san := range tlsInfo.State.PeerCertificates[0].DNSNames {
			if _, ok := allowed[san]; !ok {
				continue
			}
			if identity != "" && identity != san {
				return nil, status.Error(codes.Unauthenticated, "ambiguous Edge workload SAN")
			}
			identity = san
		}
		if identity == "" {
			return nil, status.Error(codes.PermissionDenied, "Edge workload SAN not allowlisted")
		}
		return handler(context.WithValue(ctx, peerWorkloadKey{}, identity), req)
	}
}

func PeerWorkload(ctx context.Context) (string, bool) {
	value, ok := ctx.Value(peerWorkloadKey{}).(string)
	return value, ok && value != ""
}

// AuthorizeTargetPeer binds the authenticated certificate workload to the
// target's current, unrevoked assignment. Message fields never grant identity.
func AuthorizeTargetPeer(ctx context.Context, pool *db.Pool, targetID string) error {
	workload, ok := PeerWorkload(ctx)
	if !ok || pool == nil || targetID == "" {
		return errors.New("authenticated Edge workload unavailable")
	}
	var exact bool
	if err := pool.Pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM target_assignments a
		WHERE a.target_id=$1 AND a.edge_workload_ref=$2 AND a.revoked_at_unix_ms IS NULL
		 AND a.expires_at_unix_ms>$3 AND a.assignment_generation=(SELECT max(x.assignment_generation)
		 FROM target_assignments x WHERE x.target_id=a.target_id))`, targetID, workload, time.Now().UnixMilli()).Scan(&exact); err != nil {
		return err
	}
	if !exact {
		return errors.New("mTLS workload is not current target assignee")
	}
	return nil
}
