package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAboutPrintsNotesAndSetupWithoutSecrets(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(cfg, []byte(`language = "zh"
data_dir = "`+filepath.ToSlash(dir)+`"

[[projects]]
name = "longniang"
team_name = "龙娘"
[projects.agent]
type = "codex"
[projects.agent.options]
model = "gpt-x"
work_dir = "`+filepath.ToSlash(dir)+`"
[[projects.platforms]]
type = "discord"
[projects.platforms.options]
token = "SECRET-TOKEN-123"

[[projects]]
name = "maomao"
[projects.agent]
type = "claudecode"
[projects.agent.options]
work_dir = "`+filepath.ToSlash(dir)+`"
[[projects.platforms]]
type = "discord"
[projects.platforms.options]
token = "SECRET-TOKEN-456"
`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CC_PROJECT", "maomao")
	var out bytes.Buffer
	if err := aboutCommand([]string{"--config", cfg}, &out); err != nil {
		t.Fatal(err)
	}
	s := out.String()
	for _, want := range []string{"bothouse version:", "## 本机配置", "共 2 个角色", "longniang（显示名 龙娘）：CLI codex，模型 gpt-x；平台 discord", "maomao ——就是你", "已接平台：discord"} {
		if !strings.Contains(s, want) {
			t.Errorf("about output lacks %q:\n%s", want, s)
		}
	}
	for _, unwanted := range []string{"bothouse 是什么", "LobeHub", "OpenClaw", "路线图", "https://github.com/zuiho-kai/bothouse", "SECRET-TOKEN"} {
		if strings.Contains(s, unwanted) {
			t.Errorf("about output contains %q", unwanted)
		}
	}
}
