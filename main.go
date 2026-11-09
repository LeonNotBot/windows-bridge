// Windows Bridge Core: authenticated HTTP entry point. Job execution is not yet implemented.
package main

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

func handler(token string, started time.Time) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "version": "0.1.0-dev", "api_version": "v1", "started_at": started.UTC().Format(time.RFC3339), "uptime_seconds": int(time.Since(started).Seconds())})
	})
	mux.HandleFunc("GET /v1/targets", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"targets": []any{}})
	})
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		requestID := r.Header.Get("X-Request-ID")
		if requestID != "" {
			w.Header().Set("X-Request-ID", requestID)
		}
		provided := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") || subtle.ConstantTimeCompare([]byte(provided), []byte(token)) != 1 {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"error": map[string]any{"code": "UNAUTHORIZED", "message": "Authentication required", "retryable": false}, "request_id": requestID})
			return
		}
		mux.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func main() {
	listen := flag.String("listen", "127.0.0.1:19827", "loopback listen address")
	tokenFile := flag.String("token-file", "", "path to a file containing the Bridge token")
	flag.Parse()
	if *tokenFile == "" {
		log.Fatal("-token-file is required")
	}
	b, err := os.ReadFile(*tokenFile)
	if err != nil {
		log.Fatal("cannot read token file")
	}
	token := strings.TrimSpace(string(b))
	if len(token) < 32 {
		log.Fatal("token must be at least 32 characters")
	}
	host, _, err := net.SplitHostPort(*listen)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		log.Fatal("only loopback listen addresses are supported; use a secure tunnel for remote access")
	}
	server := &http.Server{Addr: *listen, Handler: handler(token, time.Now()), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 20}
	log.Printf("Windows Bridge development server listening on %s", *listen)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}
