package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"

	"sync"
	"testing"
	"time"

	"github.com/QuantumNous/new-api/common"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestSyncCodexModelCatalog(t *testing.T) {
	var mu sync.Mutex
	status := http.StatusOK
	payload := `{"data":[{"id":"model-b"},{"id":"model-a"},{"id":"model-b"},{"id":"image","supported_endpoint_types":["image-generation"]},{"id":"step-5-preview","supported_endpoint_types":["openai-response"]}]}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, "/v1/models", r.URL.Path)
		assert.Equal(t, "Bearer test-gateway-token", r.Header.Get("Authorization"))
		mu.Lock()
		currentStatus, currentPayload := status, payload
		mu.Unlock()
		w.WriteHeader(currentStatus)
		_, _ = w.Write([]byte(currentPayload))
	}))
	defer server.Close()

	root := t.TempDir()
	keyPath := filepath.Join(root, ".codex-sync", "config", "api-key")
	outputPath := filepath.Join(root, ".codex-sync", "catalog", "models.json")
	require.NoError(t, os.MkdirAll(filepath.Dir(keyPath), 0700))
	require.NoError(t, os.MkdirAll(filepath.Dir(outputPath), 0700))
	require.NoError(t, os.WriteFile(keyPath, []byte(codexGatewayKeyPlaceholder+"\n"), 0600))
	_, _, err := syncCodexModelCatalog(context.Background(), server.Client(), server.URL+"/v1", keyPath, filepath.Join(root, "template.json"), outputPath)
	require.ErrorIs(t, err, errCodexGatewayKeyPending)
	assert.NoFileExists(t, filepath.Join(filepath.Dir(outputPath), "models.last-success"))
	require.NoError(t, os.WriteFile(keyPath, []byte("test-gateway-token\n"), 0600))
	require.NoError(t, os.WriteFile(outputPath, []byte(`{"models":[{"slug":"model-a","context_window":12345,"visibility":"hide","supported_in_api":false}]}`), 0600))
	templatePath := filepath.Join(root, ".codex-sync", "config", "template.json")
	baseURL := server.URL + "/v1"

	count, changed, err := syncCodexModelCatalog(context.Background(), server.Client(), baseURL, keyPath, templatePath, outputPath)
	require.NoError(t, err)
	assert.Equal(t, 3, count)
	assert.True(t, changed)
	first, err := os.ReadFile(outputPath)
	require.NoError(t, err)
	assert.NotContains(t, string(first), "test-gateway-token")
	var catalog struct {
		Models []map[string]any `json:"models"`
	}
	require.NoError(t, common.Unmarshal(first, &catalog))
	require.Len(t, catalog.Models, 3)
	assert.Equal(t, []string{"model-a", "model-b", "step-5-preview"}, []string{
		catalog.Models[0]["slug"].(string), catalog.Models[1]["slug"].(string), catalog.Models[2]["slug"].(string),
	})
	assert.Equal(t, float64(12345), catalog.Models[0]["context_window"])
	assert.Equal(t, "list", catalog.Models[0]["visibility"])
	assert.Equal(t, true, catalog.Models[0]["supported_in_api"])
	assert.Equal(t, "medium", catalog.Models[2]["default_reasoning_level"])
	assert.FileExists(t, filepath.Join(filepath.Dir(outputPath), "models.last-success"))

	info, err := os.Stat(outputPath)
	require.NoError(t, err)
	count, changed, err = syncCodexModelCatalog(context.Background(), server.Client(), baseURL, keyPath, templatePath, outputPath)
	require.NoError(t, err)
	assert.Equal(t, 3, count)
	assert.False(t, changed)
	unchanged, err := os.Stat(outputPath)
	require.NoError(t, err)
	assert.Equal(t, info.ModTime(), unchanged.ModTime())

	mu.Lock()
	payload = `{"data":[{"id":"model-b"}]}`
	mu.Unlock()
	count, changed, err = syncCodexModelCatalog(context.Background(), server.Client(), baseURL, keyPath, templatePath, outputPath)
	require.NoError(t, err)
	assert.Equal(t, 1, count)
	assert.True(t, changed)
	updated, err := os.ReadFile(outputPath)
	require.NoError(t, err)
	require.NoError(t, common.Unmarshal(updated, &catalog))
	require.Len(t, catalog.Models, 1)
	assert.Equal(t, "model-b", catalog.Models[0]["slug"])
	markerPath := filepath.Join(filepath.Dir(outputPath), "models.last-success")
	markerBeforeEmpty, err := os.Stat(markerPath)
	require.NoError(t, err)
	mu.Lock()
	payload = `{"data":[]}`
	mu.Unlock()
	_, _, err = syncCodexModelCatalog(context.Background(), server.Client(), baseURL, keyPath, templatePath, outputPath)
	require.ErrorIs(t, err, errCodexGatewayModelsPending)
	markerAfterEmpty, err := os.Stat(markerPath)
	require.NoError(t, err)
	assert.Equal(t, markerBeforeEmpty.ModTime(), markerAfterEmpty.ModTime())
	stillPopulated, err := os.ReadFile(outputPath)
	require.NoError(t, err)
	assert.Equal(t, updated, stillPopulated)

	mu.Lock()
	status = http.StatusUnauthorized
	mu.Unlock()
	_, _, err = syncCodexModelCatalog(context.Background(), server.Client(), baseURL, keyPath, templatePath, outputPath)
	require.ErrorContains(t, err, "HTTP 401")
	assert.NotContains(t, err.Error(), "test-gateway-token")
	afterFailure, err := os.ReadFile(outputPath)
	require.NoError(t, err)
	assert.Equal(t, updated, afterFailure)
}

func TestCodexModelSyncDelay(t *testing.T) {
	outputPath := filepath.Join(t.TempDir(), "models.json")
	now := time.Date(2026, time.September, 24, 0, 0, 0, 0, time.UTC)
	assert.Zero(t, codexModelSyncDelay(outputPath, now))
	markerPath := filepath.Join(filepath.Dir(outputPath), "models.last-success")
	require.NoError(t, os.WriteFile(outputPath, []byte(`{"models":[]}`), 0600))
	require.NoError(t, os.WriteFile(markerPath, nil, 0600))
	require.NoError(t, os.Chtimes(markerPath, now.Add(-time.Hour), now.Add(-time.Hour)))
	assert.Equal(t, 3*time.Hour, codexModelSyncDelay(outputPath, now))
	assert.Zero(t, codexModelSyncDelay(outputPath, now.Add(3*time.Hour)))
	require.NoError(t, os.Remove(outputPath))
	assert.Zero(t, codexModelSyncDelay(outputPath, now))
}

func TestSyncCodexModelCatalogAddsDocumentedReasoningEfforts(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[{"id":"gpt-5.5"},{"id":"gpt-5.6"},{"id":"gpt-5.6-luna"},{"id":"gpt-6-astra"},{"id":"gpt-6-sol"},{"id":"gpt-reserve"}]}`))
	}))
	defer server.Close()
	root := t.TempDir()
	keyPath := filepath.Join(root, "api-key")
	outputPath := filepath.Join(root, "models.json")
	require.NoError(t, os.WriteFile(keyPath, []byte("test-gateway-token\n"), 0600))
	require.NoError(t, os.WriteFile(outputPath, []byte(`{"models":[{"slug":"gpt-6-astra","default_reasoning_level":"none","supported_reasoning_levels":[]},{"slug":"gpt-5.6","default_reasoning_level":"none","supported_reasoning_levels":[{"effort":"high","description":"Custom"}]},{"slug":"gpt-reserve","default_reasoning_level":"high","supported_reasoning_levels":[{"effort":"high","description":"Custom"}]}]}`), 0600))
	count, changed, err := syncCodexModelCatalog(context.Background(), server.Client(), server.URL+"/v1", keyPath, filepath.Join(root, "missing-template.json"), outputPath)
	require.NoError(t, err)
	assert.True(t, changed)
	assert.Equal(t, 6, count)
	content, err := os.ReadFile(outputPath)
	require.NoError(t, err)
	var catalog struct {
		Models []map[string]any `json:"models"`
	}
	require.NoError(t, common.Unmarshal(content, &catalog))
	byName := make(map[string]map[string]any, len(catalog.Models))
	for _, profile := range catalog.Models {
		byName[profile["slug"].(string)] = profile
	}
	cases := []struct {
		name    string
		efforts []string
	}{
		{"gpt-5.5", []string{"none", "low", "medium", "high", "xhigh"}},
		{"gpt-5.6", []string{"high"}},
		{"gpt-5.6-luna", []string{"none", "low", "medium", "high", "xhigh", "max"}},
		{"gpt-6-astra", []string{"low", "medium", "high", "xhigh", "max"}},
		{"gpt-6-sol", []string{"none", "low", "medium", "high", "xhigh", "max"}},
		{"gpt-reserve", []string{"high"}},
	}
	for _, test := range cases {
		var efforts []string
		for _, value := range byName[test.name]["supported_reasoning_levels"].([]any) {
			efforts = append(efforts, value.(map[string]any)["effort"].(string))
		}
		assert.Equal(t, test.efforts, efforts, test.name)
	}
	assert.Equal(t, "medium", byName["gpt-6-astra"]["default_reasoning_level"])
	assert.Equal(t, "high", byName["gpt-5.6"]["default_reasoning_level"])
	assert.Equal(t, "high", byName["gpt-reserve"]["default_reasoning_level"])
}

func TestSyncCodexModelCatalogRejectsInvalidResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[{"id":"model-a","supported_endpoint_types":null}]}`))
	}))
	defer server.Close()
	root := t.TempDir()
	keyPath := filepath.Join(root, "api-key")
	outputPath := filepath.Join(root, "models.json")
	require.NoError(t, os.WriteFile(keyPath, []byte("test-gateway-token\n"), 0600))
	require.NoError(t, os.WriteFile(outputPath, []byte(`{"models":[]}`), 0600))
	_, _, err := syncCodexModelCatalog(context.Background(), server.Client(), server.URL+"/v1", keyPath, filepath.Join(root, "missing-template.json"), outputPath)
	require.ErrorContains(t, err, "invalid gateway model endpoints")
	unchanged, err := os.ReadFile(outputPath)
	require.NoError(t, err)
	assert.Equal(t, `{"models":[]}`, string(unchanged))
}
