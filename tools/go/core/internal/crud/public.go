package crud

import (
	"fmt"
	"net/http"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Serve is handle as a public route's handler (auth.Public, behind the person-auth gate):
// its answer is written as a CRUD route's is (a value as JSON with 200, a *Reply as it
// says), and its error as the same problem. A body is at most maxBody bytes when maxBody
// is above 0. The answer is never cached.
func Serve(handle func(r *http.Request) (any, error), maxBody int64) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if maxBody > 0 && r.ContentLength > maxBody {
			httpx.WriteProblem(w, r, &httpx.Problem{
				Status: http.StatusRequestEntityTooLarge,
				Detail: fmt.Sprintf("a body is at most %d bytes", maxBody),
			})
			return
		}
		if maxBody > 0 {
			r.Body = http.MaxBytesReader(nil, r.Body, maxBody)
		}
		w.Header().Set("Cache-Control", "no-store")
		answer, err := handle(r)
		if err == nil {
			err = writeAnswer(w, http.StatusOK, answer)
		}
		if err != nil {
			httpx.WriteProblem(w, r, problemOf(r, err))
		}
	})
}
