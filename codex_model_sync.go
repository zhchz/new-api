package main

import (
	"bytes"
	"context"
	_ "embed"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/QuantumNous/new-api/common"
)

const codexCatalogResponseLimit = 8 << 20
const codexModelSyncInterval = 4 * time.Hour

const codexGatewayKeyPlaceholder = "REPLACE_WITH_NEW_API_KEY"

var errCodexGatewayKeyPending = errors.New("gateway API key has not been configured")
var errCodexGatewayModelsPending = errors.New("gateway has no usable models for this key")
var errCodexGatewayUnauthorized = errors.New("gateway key is not authorized")

const codexModelSyncEnabledFile = "auto-sync-enabled"

//go:embed bin/codex-model-template.json
var codexReasoningTemplate []byte

var codexKnownGPTModelPattern = regexp.MustCompile(`^gpt-(\d+)(?:\.(\d+))?(?:-(luna|sol|terra|astra))?$`)

// Codex launchers call this after the gateway key and channels are configured.
// The readiness mode never writes a catalog or activates automatic sync.
func runCodexModelSync(port string, checkOnly bool) error {
	portNumber, err := strconv.Atoi(port)
	if err != nil || portNumber < 1 || portNumber > 65535 {
		return errors.New("invalid gateway port")
	}
	executable, err := os.Executable()
	if err != nil {
		return errors.New("executable path unavailable")
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

	if checkOnly {
		_, err := fetchCodexGatewayModelNames(context.Background(), client, baseURL, keyPath)
		return err
	}
	count, changed, err := syncCodexModelCatalog(context.Background(), client, baseURL, keyPath, templatePath, outputPath)
	if err != nil {
		return err
	}
	if changed {
		fmt.Printf("Codex model catalog updated: %d models\n", count)
	} else {
		fmt.Printf("Codex model catalog checked: %d models\n", count)
	}
	return enableCodexModelSync(root)
}

func enableCodexModelSync(root string) error {
	enabledPath := filepath.Join(root, ".codex-sync", "config", codexModelSyncEnabledFile)
	if err := os.WriteFile(enabledPath, nil, 0600); err != nil {
		return errors.New("cannot enable automatic Codex model sync")
	}
	return nil
}

// The gateway only watches for activation. A Codex launch creates the marker
// after its first successful sync; no model request happens before that launch.
func startWindowsCodexModelSyncWatcher(port string) {
	if runtime.GOOS != "windows" {
		return
	}
	executable, err := os.Executable()
	if err != nil {
		return
	}
	root := filepath.Dir(executable)
	enabledPath := filepath.Join(root, ".codex-sync", "config", codexModelSyncEnabledFile)
	keyPath := filepath.Join(root, ".codex-sync", "config", "api-key")
	templatePath := filepath.Join(root, ".codex-sync", "config", "template.json")
	outputPath := filepath.Join(root, ".codex-sync", "catalog", "models.json")
	client := &http.Client{
		Timeout:   15 * time.Second,
		Transport: &http.Transport{Proxy: nil},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return errors.New("gateway redirect refused")
		},
	}
	baseURL := "http://127.0.0.1:" + port + "/v1"
	go func() {
		for {
			if _, err := os.Stat(enabledPath); err != nil {
				time.Sleep(10 * time.Second)
				continue
			}
			if delay := codexModelSyncDelay(outputPath, time.Now()); delay > 0 {
				time.Sleep(min(delay, time.Minute))
				continue
			}
			count, changed, err := syncCodexModelCatalog(context.Background(), client, baseURL, keyPath, templatePath, outputPath)
			if errors.Is(err, errCodexGatewayKeyPending) || errors.Is(err, errCodexGatewayModelsPending) || errors.Is(err, errCodexGatewayUnauthorized) {
				common.SysLog("Codex model sync paused; check the key and channels, then restart Codex")
				_ = os.Remove(enabledPath)
				continue
			}
			if err != nil {
				common.SysLog("Codex model sync failed: " + err.Error() + "; previous catalog retained")
				time.Sleep(codexModelSyncInterval)
				continue
			}
			if changed {
				common.SysLog(fmt.Sprintf("Codex model catalog updated: %d models; restart Codex to load it", count))
			}
		}
	}()
}

func readCodexGatewayKey(keyPath string) (string, error) {
	keyFile, err := os.ReadFile(keyPath)
	if err != nil {
		return "", errCodexGatewayKeyPending
	}
	if len(keyFile) > 4096 {
		return "", errors.New("gateway key file too large")
	}
	apiKey := strings.TrimSpace(strings.TrimPrefix(string(keyFile), "\ufeff"))
	if apiKey == "" || apiKey == codexGatewayKeyPlaceholder {
		return "", errCodexGatewayKeyPending
	}
	if strings.ContainsAny(apiKey, "\r\n") {
		return "", errors.New("NEW_API_KEY is missing or invalid")
	}
	return apiKey, nil
}

func codexModelSyncDelay(outputPath string, now time.Time) time.Duration {
	if _, err := os.Stat(outputPath); err != nil {
		return 0
	}
	markerPath := strings.TrimSuffix(outputPath, filepath.Ext(outputPath)) + ".last-success"
	marker, err := os.Stat(markerPath)
	if err != nil {
		return 0
	}
	return max(0, min(codexModelSyncInterval, codexModelSyncInterval-now.Sub(marker.ModTime())))
}

func fetchCodexGatewayModelNames(ctx context.Context, client *http.Client, baseURL, keyPath string) ([]string, error) {
	apiKey, err := readCodexGatewayKey(keyPath)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(baseURL, "/")+"/models", nil)
	if err != nil {
		return nil, errors.New("invalid gateway URL")
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("gateway request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			return nil, fmt.Errorf("gateway HTTP %d: %w", resp.StatusCode, errCodexGatewayUnauthorized)
		}
		return nil, fmt.Errorf("gateway HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, codexCatalogResponseLimit+1))
	if err != nil || len(body) > codexCatalogResponseLimit {
		return nil, errors.New("gateway model response unreadable or too large")
	}
	var payload map[string]any
	if err := common.Unmarshal(body, &payload); err != nil || payload == nil || payload["success"] == false {
		return nil, errors.New("invalid gateway model response")
	}
	upstream, ok := payload["data"].([]any)
	if !ok {
		return nil, errors.New("invalid gateway model list")
	}
	names := make(map[string]struct{}, len(upstream))
	for _, item := range upstream {
		entry, ok := item.(map[string]any)
		if !ok {
			return nil, errors.New("invalid gateway model entry")
		}
		id, ok := entry["id"].(string)
		name := strings.TrimSpace(id)
		if !ok || name == "" || strings.IndexFunc(name, func(r rune) bool { return r < 32 }) >= 0 {
			return nil, errors.New("invalid gateway model identifier")
		}
		if rawEndpoints, present := entry["supported_endpoint_types"]; present {
			endpoints, ok := rawEndpoints.([]any)
			if !ok {
				return nil, errors.New("invalid gateway model endpoints")
			}
			for _, value := range endpoints {
				if _, ok := value.(string); !ok {
					return nil, errors.New("invalid gateway model endpoints")
				}
			}
		}
		// Native endpoints do not account for gateway-side protocol conversion.
		// Keep every model exposed to this key in the client catalog.
		names[name] = struct{}{}
	}
	modelNames := make([]string, 0, len(names))
	for name := range names {
		modelNames = append(modelNames, name)
	}
	if len(modelNames) == 0 {
		return nil, errCodexGatewayModelsPending
	}
	slices.Sort(modelNames)
	return modelNames, nil
}

// codexReasoningModelParts identifies the version and known reasoning family.
func codexReasoningModelParts(name string) (major, minor int, family string, known bool) {
	parts := codexKnownGPTModelPattern.FindStringSubmatch(name)
	if parts == nil {
		return 0, 0, "", false
	}
	major, err := strconv.Atoi(parts[1])
	if err != nil {
		return 0, 0, "", false
	}
	if parts[2] != "" {
		minor, err = strconv.Atoi(parts[2])
		if err != nil {
			return 0, 0, "", false
		}
	}
	family = parts[3]
	known = major == 5 && minor == 5 && family == "" ||
		major == 5 && minor >= 6 && family != "astra" || major >= 6
	return major, minor, family, known
}

func syncCodexModelCatalog(ctx context.Context, client *http.Client, baseURL, keyPath, templatePath, outputPath string) (int, bool, error) {
	modelNames, err := fetchCodexGatewayModelNames(ctx, client, baseURL, keyPath)
	if err != nil {
		return 0, false, err
	}

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
	// Managed reasoning fields always win over stale machine-local catalogs.
	// Other metadata and unknown models remain untouched.
	var managed struct {
		Models                  []map[string]any  `json:"models"`
		ReasoningFamilyDefaults map[string]string `json:"reasoning_family_defaults"`
	}
	if err := common.Unmarshal(codexReasoningTemplate, &managed); err != nil {
		return 0, false, errors.New("invalid bundled catalog template")
	}
	for _, definition := range managed.Models {
		name := definition["slug"].(string)
		profile := make(map[string]any)
		maps.Copy(profile, profiles[name])
		maps.Copy(profile, definition)
		profiles[name] = profile
	}
	models := make([]map[string]any, 0, len(modelNames))
	for priority, name := range modelNames {
		profile := map[string]any{
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
		maps.Copy(profile, profiles[name])
		major, minor, family, known := codexReasoningModelParts(name)
		if known {
			levels, ok := profile["supported_reasoning_levels"].([]any)
			if !ok || len(levels) == 0 {
				previousMinor := -1
				var previous map[string]any
				for slug, candidate := range profiles {
					candidateMajor, candidateMinor, candidateFamily, candidateKnown := codexReasoningModelParts(slug)
					if !candidateKnown || candidateMajor != major || candidateFamily != family ||
						candidateMinor >= minor || candidateMinor <= previousMinor {
						continue
					}
					candidateLevels, valid := candidate["supported_reasoning_levels"].([]any)
					if !valid || len(candidateLevels) == 0 {
						continue
					}
					previous, previousMinor = candidate, candidateMinor
					levels, ok = candidateLevels, true
				}
				if previous == nil {
					fallbackFamily := family
					if fallbackFamily == "" {
						fallbackFamily = "standard"
					}
					previous = profiles[managed.ReasoningFamilyDefaults[fallbackFamily]]
					levels, ok = previous["supported_reasoning_levels"].([]any)
				}
				profile["supported_reasoning_levels"] = levels
				if profile["default_reasoning_level"] == nil {
					profile["default_reasoning_level"] = previous["default_reasoning_level"]
				}
			}
			availableEfforts := make([]string, 0, len(levels))
			if ok && len(levels) > 0 {
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
				return 0, false, errors.New("invalid catalog reasoning levels")
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
