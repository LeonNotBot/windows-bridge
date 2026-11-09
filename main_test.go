package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestAuthenticationAppliesToHealthAndTargets(t *testing.T) {
	h := handler("01234567890123456789012345678901", time.Now())
	for _, path := range []string{"/v1/health", "/v1/targets"} {
		for _, authorized := range []bool{false, true} {
			req := httptest.NewRequest(http.MethodGet, path, nil)
			req.Header.Set("X-Request-ID", "test-id")
			if authorized {
				req.Header.Set("Authorization", "Bearer 01234567890123456789012345678901")
			}
			r := httptest.NewRecorder()
			h.ServeHTTP(r, req)
			want := http.StatusUnauthorized
			if authorized {
				want = http.StatusOK
			}
			if r.Code != want || r.Header().Get("X-Request-ID") != "test-id" {
				t.Fatalf("%s authorized=%v: status %d", path, authorized, r.Code)
			}
		}
	}
}
