package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

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
	payload := `{"data":[{"id":"model-b"},{"id":"model-a"},{"id":"model-b"},{"id":"image","supported_endpoint_types":["image-generation"]},{"id":"step-5-preview","supported_endpoint_types":["openai-response"]},{"id":"deepseek-flash","supported_endpoint_types":["openai"]},{"id":"deepseek-v4-pro","supported_endpoint_types":["openai"]}]}`
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
	assert.Equal(t, 6, count)
	assert.True(t, changed)
	first, err := os.ReadFile(outputPath)
	require.NoError(t, err)
	assert.NotContains(t, string(first), "test-gateway-token")
	var catalog struct {
		Models []map[string]any `json:"models"`
	}
	require.NoError(t, common.Unmarshal(first, &catalog))
	require.Len(t, catalog.Models, 6)
	names := make([]string, 0, len(catalog.Models))
	profiles := make(map[string]map[string]any, len(catalog.Models))
	for _, profile := range catalog.Models {
		name := profile["slug"].(string)
		names = append(names, name)
		profiles[name] = profile
		assert.Equal(t, "list", profile["visibility"])
		assert.Equal(t, true, profile["supported_in_api"])
	}
	assert.Equal(t, []string{"deepseek-flash", "deepseek-v4-pro", "image", "model-a", "model-b", "step-5-preview"}, names)
	assert.Equal(t, float64(12345), profiles["model-a"]["context_window"])
	assert.Equal(t, "medium", profiles["step-5-preview"]["default_reasoning_level"])
	assert.FileExists(t, filepath.Join(filepath.Dir(outputPath), "models.last-success"))

	info, err := os.Stat(outputPath)
	require.NoError(t, err)
	count, changed, err = syncCodexModelCatalog(context.Background(), server.Client(), baseURL, keyPath, templatePath, outputPath)
	require.NoError(t, err)
	assert.Equal(t, 6, count)
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

func TestSyncCodexModelCatalogAddsReasoningEffortsForNewFamilyVersions(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"data":[{"id":"gpt-5.5"},{"id":"gpt-5.6"},{"id":"gpt-5.6-luna"},{"id":"gpt-6-astra"},{"id":"gpt-6-sol"},{"id":"gpt-6.1-sol"},{"id":"gpt-6.2-sol"},{"id":"gpt-6.1-astra"},{"id":"gpt-6.1-image"},{"id":"model-custom"}]}`))
	}))
	defer server.Close()
	root := t.TempDir()
	keyPath := filepath.Join(root, "api-key")
	outputPath := filepath.Join(root, "models.json")
	require.NoError(t, os.WriteFile(keyPath, []byte("test-gateway-token\n"), 0600))
	require.NoError(t, os.WriteFile(outputPath, []byte(`{"models":[{"slug":"gpt-6-astra","default_reasoning_level":"none","supported_reasoning_levels":[]},{"slug":"gpt-6-sol","default_reasoning_level":"low","supported_reasoning_levels":[{"effort":"low","description":"Custom low"},{"effort":"medium","description":"Custom medium"},{"effort":"high","description":"Custom high"},{"effort":"xhigh","description":"Custom xhigh"},{"effort":"max","description":"Custom max"},{"effort":"ultra","description":"Custom ultra"}]},{"slug":"gpt-5.6","default_reasoning_level":"none","supported_reasoning_levels":[{"effort":"high","description":"Custom"}]},{"slug":"model-custom","default_reasoning_level":"high","supported_reasoning_levels":[{"effort":"high","description":"Custom"}]}]}`), 0600))
	count, changed, err := syncCodexModelCatalog(context.Background(), server.Client(), server.URL+"/v1", keyPath, filepath.Join(root, "missing-template.json"), outputPath)
	require.NoError(t, err)
	assert.True(t, changed)
	assert.Equal(t, 10, count)
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
		{"gpt-5.5", []string{"low", "medium", "high", "xhigh"}},
		{"gpt-5.6", []string{"high"}},
		{"gpt-5.6-luna", []string{"low", "medium", "high", "xhigh", "max"}},
		{"gpt-6-astra", []string{"low", "medium", "high", "xhigh", "max", "ultra"}},
		{"gpt-6-sol", []string{"low", "medium", "high", "xhigh", "max", "ultra"}},
		{"gpt-6.1-sol", []string{"low", "medium", "high", "xhigh", "max", "ultra"}},
		{"gpt-6.2-sol", []string{"low", "medium", "high", "xhigh", "max", "ultra"}},
		{"gpt-6.1-astra", []string{"low", "medium", "high", "xhigh", "max", "ultra"}},
		{"gpt-6.1-image", []string{}},
		{"model-custom", []string{"high"}},
	}
	for _, test := range cases {
		efforts := []string{}
		for _, value := range byName[test.name]["supported_reasoning_levels"].([]any) {
			efforts = append(efforts, value.(map[string]any)["effort"].(string))
		}
		assert.Equal(t, test.efforts, efforts, test.name)
	}
	assert.Equal(t, "low", byName["gpt-6-astra"]["default_reasoning_level"])
	assert.Equal(t, "medium", byName["gpt-6.1-sol"]["default_reasoning_level"])
	assert.Equal(t, "Ultra reasoning for the most demanding tasks", byName["gpt-6.1-sol"]["supported_reasoning_levels"].([]any)[5].(map[string]any)["description"])
	assert.Equal(t, "high", byName["gpt-5.6"]["default_reasoning_level"])
	assert.Equal(t, "high", byName["model-custom"]["default_reasoning_level"])
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

func TestCodexModelCatalogMatchesPythonForFreshAndStaleTemplates(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("Python 3 is required for cross-platform catalog verification")
	}
	var bundled struct {
		Models []map[string]any `json:"models"`
	}
	require.NoError(t, common.Unmarshal(codexReasoningTemplate, &bundled))
	names := make([]string, 0, len(bundled.Models)+3)
	entries := make([]map[string]any, 0, len(bundled.Models)+3)
	staleModels := make([]map[string]any, 0, len(bundled.Models)+1)
	for _, profile := range bundled.Models {
		name := profile["slug"].(string)
		names = append(names, name)
		entries = append(entries, map[string]any{"id": name})
		staleModels = append(staleModels, map[string]any{
			"slug":                       name,
			"context_window":             123456,
			"base_instructions":          "Existing instructions",
			"default_reasoning_level":    "none",
			"supported_reasoning_levels": []map[string]string{{"effort": "xhigh", "description": "Stale"}},
		})
	}
	for _, name := range []string{"gpt-6.2-sol", "gpt-6.1-astra", "model-custom"} {
		names = append(names, name)
		entries = append(entries, map[string]any{"id": name})
	}
	staleModels = append(staleModels, map[string]any{
		"slug":                       "model-custom",
		"default_reasoning_level":    "high",
		"supported_reasoning_levels": []map[string]string{{"effort": "high", "description": "Custom"}},
	})
	payload, err := common.Marshal(map[string]any{"data": entries})
	require.NoError(t, err)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, "Bearer test-gateway-token", r.Header.Get("Authorization"))
		_, _ = w.Write(payload)
	}))
	defer server.Close()
	workingDirectory, err := os.Getwd()
	require.NoError(t, err)
	for _, source := range []string{"fresh", "stale", "previous-catalog"} {
		t.Run(source, func(t *testing.T) {
			root := t.TempDir()
			keyPath := filepath.Join(root, "api-key")
			templatePath := filepath.Join(root, "template.json")
			outputPath := filepath.Join(root, "models.json")
			require.NoError(t, os.WriteFile(keyPath, []byte("test-gateway-token\n"), 0600))
			template := map[string]any{"models": []map[string]any{}}
			if source != "fresh" {
				template["models"] = staleModels
			}
			encodedTemplate, err := common.Marshal(template)
			require.NoError(t, err)
			target := templatePath
			if source == "previous-catalog" {
				target = outputPath
			}
			require.NoError(t, os.WriteFile(target, encodedTemplate, 0600))
			count, changed, err := syncCodexModelCatalog(context.Background(), server.Client(), server.URL+"/v1", keyPath, templatePath, outputPath)
			require.NoError(t, err)
			assert.True(t, changed)
			assert.Equal(t, len(names), count)
			actual, err := os.ReadFile(outputPath)
			require.NoError(t, err)
			input, err := common.Marshal(map[string]any{"names": names, "template": template})
			require.NoError(t, err)
			command := exec.Command(python, "-B", "-c", `import json, sys
from codex_model_sync import build_catalog
request = json.load(sys.stdin)
print(json.dumps(build_catalog(request["names"], request["template"])))`)
			command.Dir = filepath.Join(workingDirectory, "bin")
			command.Stdin = strings.NewReader(string(input))
			expected, err := command.CombinedOutput()
			require.NoError(t, err, string(expected))
			assert.JSONEq(t, string(expected), string(actual))
			keyAfter, err := os.ReadFile(keyPath)
			require.NoError(t, err)
			assert.Equal(t, "test-gateway-token\n", string(keyAfter))
			assert.NotContains(t, string(actual), "test-gateway-token")
		})
	}
}
