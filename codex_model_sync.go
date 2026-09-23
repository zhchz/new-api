package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/QuantumNous/new-api/common"
)

const codexCatalogResponseLimit = 8 << 20

const codexGatewayKeyPlaceholder = "REPLACE_WITH_NEW_API_KEY"

var errCodexGatewayKeyPending = errors.New("gateway API key has not been configured")

// Windows uses the gateway executable as the only service entry point.
// Linux keeps using the optional codex-model-sync Compose service.
func startWindowsCodexModelSync(port string) {
	if runtime.GOOS != "windows" {
		return
	}
	portNumber, err := strconv.Atoi(port)
	if err != nil || portNumber < 1 || portNumber > 65535 {
		common.SysLog("Codex model sync disabled: invalid gateway port")
		return
	}
	executable, err := os.Executable()
	if err != nil {
		common.SysLog("Codex model sync disabled: executable path unavailable")
		return
	}
	root := filepath.Dir(executable)
	keyPath := filepath.Join(root, ".codex-sync", "config", "api-key")
	templatePath := filepath.Join(root, ".codex-sync", "config", "template.json")
	outputPath := filepath.Join(root, ".codex-sync", "catalog", "models.json")
	baseURL := "http://127.0.0.1:" + port + "/v1"
	client := &http.Client{
		Timeout:   15 * time.Second,
		Transport: &http.Transport{Proxy: nil},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return errors.New("gateway redirect refused")
		},
	}

	go func() {
		for {
			count, changed, err := syncCodexModelCatalog(context.Background(), client, baseURL, keyPath, templatePath, outputPath)
			if errors.Is(err, errCodexGatewayKeyPending) {
				time.Sleep(10 * time.Second)
				continue
			}
			if err != nil {
				common.SysLog("Codex model sync failed: " + err.Error() + "; previous catalog retained")
				time.Sleep(5 * time.Minute)
				continue
			}
			if changed {
				common.SysLog(fmt.Sprintf("Codex model catalog updated: %d models; restart Codex to load it", count))
			}
			time.Sleep(time.Hour)
		}
	}()
}

func syncCodexModelCatalog(ctx context.Context, client *http.Client, baseURL, keyPath, templatePath, outputPath string) (int, bool, error) {
	keyFile, err := os.ReadFile(keyPath)
	if err != nil {
		return 0, false, errors.New("gateway key file unavailable")
	}
	if len(keyFile) > 4096 {
		return 0, false, errors.New("gateway key file too large")
	}
	apiKey := strings.TrimSpace(strings.TrimPrefix(string(keyFile), "\ufeff"))
	if apiKey == "" || apiKey == codexGatewayKeyPlaceholder {
		return 0, false, errCodexGatewayKeyPending
	}
	if strings.ContainsAny(apiKey, "\r\n") {
		return 0, false, errors.New("NEW_API_KEY is missing or invalid")
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+"/models", nil)
	if err != nil {
		return 0, false, errors.New("invalid gateway URL")
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return 0, false, fmt.Errorf("gateway request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, false, fmt.Errorf("gateway HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, codexCatalogResponseLimit+1))
	if err != nil || len(body) > codexCatalogResponseLimit {
		return 0, false, errors.New("gateway model response unreadable or too large")
	}
	var payload map[string]any
	if err := common.Unmarshal(body, &payload); err != nil || payload == nil || payload["success"] == false {
		return 0, false, errors.New("invalid gateway model response")
	}
	upstream, ok := payload["data"].([]any)
	if !ok {
		return 0, false, errors.New("invalid gateway model list")
	}
	names := make(map[string]struct{}, len(upstream))
	for _, item := range upstream {
		entry, ok := item.(map[string]any)
		if !ok {
			return 0, false, errors.New("invalid gateway model entry")
		}
		id, ok := entry["id"].(string)
		name := strings.TrimSpace(id)
		if !ok || name == "" || strings.IndexFunc(name, func(r rune) bool { return r < 32 }) >= 0 {
			return 0, false, errors.New("invalid gateway model identifier")
		}
		if rawEndpoints, present := entry["supported_endpoint_types"]; present {
			endpoints, ok := rawEndpoints.([]any)
			if !ok {
				return 0, false, errors.New("invalid gateway model endpoints")
			}
			supported := len(endpoints) == 0
			for _, value := range endpoints {
				endpoint, ok := value.(string)
				if !ok {
					return 0, false, errors.New("invalid gateway model endpoints")
				}
				if endpoint == "openai-response" {
					supported = true
				}
			}
			if !supported {
				continue
			}
		}
		names[name] = struct{}{}
	}
	modelNames := make([]string, 0, len(names))
	for name := range names {
		modelNames = append(modelNames, name)
	}
	slices.Sort(modelNames)

	templateBytes, err := os.ReadFile(templatePath)
	if errors.Is(err, os.ErrNotExist) {
		templateBytes, err = os.ReadFile(outputPath)
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return 0, false, errors.New("catalog template unreadable")
	}
	profiles := make(map[string]map[string]any)
	if len(templateBytes) > 0 {
		var template map[string]any
		if err := common.Unmarshal(templateBytes, &template); err != nil {
			return 0, false, errors.New("invalid catalog template")
		}
		rawModels, ok := template["models"].([]any)
		if !ok {
			return 0, false, errors.New("invalid catalog template")
		}
		for _, item := range rawModels {
			profile, ok := item.(map[string]any)
			if !ok {
				return 0, false, errors.New("invalid catalog template model")
			}
			name, ok := profile["slug"].(string)
			if !ok || name == "" {
				return 0, false, errors.New("invalid catalog template model")
			}
			profiles[name] = profile
		}
	}
	models := make([]map[string]any, 0, len(modelNames))
	for priority, name := range modelNames {
		profile := profiles[name]
		if profile == nil {
			profile = map[string]any{
				"slug":                         name,
				"display_name":                 name,
				"description":                  "Model available through New API",
				"default_reasoning_level":      nil,
				"supported_reasoning_levels":   []any{},
				"shell_type":                   "unified_exec",
				"base_instructions":            "You are a coding assistant. Follow the user's instructions and the project's conventions.",
				"supports_reasoning_summaries": false,
				"support_verbosity":            false,
				"supports_parallel_tool_calls": false,
				"input_modalities":             []string{"text"},
				"truncation_policy":            map[string]any{"mode": "tokens", "limit": 10000},
				"experimental_supported_tools": []any{},
			}
			if name == "step-5-preview" {
				profile["default_reasoning_level"] = "medium"
				profile["supported_reasoning_levels"] = []map[string]string{
					{"effort": "low", "description": "Faster responses with lighter reasoning"},
					{"effort": "medium", "description": "Balanced reasoning for everyday tasks"},
					{"effort": "high", "description": "Deeper reasoning for complex tasks"},
				}
			}
		}
		var documentedEfforts []string
		switch name {
		case "gpt-5.5":
			documentedEfforts = []string{"none", "low", "medium", "high", "xhigh"}
		case "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra",
			"gpt-6-luna", "gpt-6-sol":
			documentedEfforts = []string{"none", "low", "medium", "high", "xhigh", "max"}
		case "gpt-6-astra":
			documentedEfforts = []string{"low", "medium", "high", "xhigh", "max"}
		}
		if len(documentedEfforts) > 0 {
			levels, ok := profile["supported_reasoning_levels"].([]any)
			availableEfforts := documentedEfforts
			if ok && len(levels) > 0 {
				availableEfforts = make([]string, 0, len(levels))
				for _, value := range levels {
					level, valid := value.(map[string]any)
					if !valid {
						return 0, false, errors.New("invalid catalog reasoning level")
					}
					effort, valid := level["effort"].(string)
					if !valid || effort == "" {
						return 0, false, errors.New("invalid catalog reasoning level")
					}
					availableEfforts = append(availableEfforts, effort)
				}
			} else {
				descriptions := map[string]string{
					"none":   "No reasoning",
					"low":    "Faster responses with lighter reasoning",
					"medium": "Balanced reasoning for everyday tasks",
					"high":   "Deeper reasoning for complex tasks",
					"xhigh":  "Extended reasoning for difficult tasks",
					"max":    "Maximum reasoning for the hardest tasks",
				}
				levels := make([]map[string]string, 0, len(documentedEfforts))
				for _, effort := range documentedEfforts {
					levels = append(levels, map[string]string{"effort": effort, "description": descriptions[effort]})
				}
				profile["supported_reasoning_levels"] = levels
			}
			if selected, ok := profile["default_reasoning_level"].(string); !ok || !slices.Contains(availableEfforts, selected) {
				if slices.Contains(availableEfforts, "medium") {
					profile["default_reasoning_level"] = "medium"
				} else {
					profile["default_reasoning_level"] = availableEfforts[0]
				}
			}
		}
		profile["visibility"] = "list"
		profile["supported_in_api"] = true
		profile["priority"] = priority
		models = append(models, profile)
	}
	encoded, err := common.Marshal(map[string]any{"models": models})
	if err != nil {
		return 0, false, errors.New("catalog encoding failed")
	}
	encoded, err = common.IndentJson(encoded)
	if err != nil {
		return 0, false, errors.New("catalog formatting failed")
	}
	encoded = append(encoded, '\n')
	existing, err := os.ReadFile(outputPath)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return 0, false, errors.New("catalog unreadable")
	}
	changed := !bytes.Equal(existing, encoded)
	if changed {
		if err := os.MkdirAll(filepath.Dir(outputPath), 0700); err != nil {
			return 0, false, errors.New("catalog directory unwritable")
		}
		file, err := os.CreateTemp(filepath.Dir(outputPath), ".models-*")
		if err != nil {
			return 0, false, errors.New("catalog temporary file unavailable")
		}
		defer os.Remove(file.Name())
		if _, err := file.Write(encoded); err != nil {
			file.Close()
			return 0, false, errors.New("catalog write failed")
		}
		if err := file.Sync(); err != nil {
			file.Close()
			return 0, false, errors.New("catalog flush failed")
		}
		if err := file.Close(); err != nil {
			return 0, false, errors.New("catalog close failed")
		}
		if err := os.Rename(file.Name(), outputPath); err != nil {
			return 0, false, errors.New("catalog replace failed")
		}
	}
	markerPath := strings.TrimSuffix(outputPath, filepath.Ext(outputPath)) + ".last-success"
	if err := os.WriteFile(markerPath, nil, 0600); err != nil {
		return 0, changed, errors.New("catalog success marker unwritable")
	}
	return len(modelNames), changed, nil
}
