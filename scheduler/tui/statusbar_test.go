package main

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/muesli/termenv"
)

func TestStatusBarViewShowsTxAndRespectsWidth(t *testing.T) {
	// go test isn't a real TTY, so lipgloss's own terminal detection
	// would otherwise strip all color codes and make the up/down renders
	// identical -- force a profile so the color this test actually cares
	// about gets emitted.
	prev := lipgloss.ColorProfile()
	lipgloss.SetColorProfile(termenv.TrueColor)
	defer lipgloss.SetColorProfile(prev)

	up := statusBarView(40, true)
	if !strings.Contains(up, "Tx") {
		t.Errorf("expected status bar to contain \"Tx\", got %q", up)
	}
	if got := lipglossVisibleWidth(up); got != 40 {
		t.Errorf("expected rendered width 40, got %d", got)
	}
	if !strings.Contains(up, "48;5;"+styleStatusBarBGColor) {
		t.Errorf("expected the dark-grey background escape code in the rendered bar, got %q", up)
	}
	if idx := lipglossVisibleWidth(strings.SplitN(stripANSI(up), "Tx", 2)[0]); idx > 4 {
		t.Errorf("expected \"Tx\" near the left edge, found it at visible offset %d in %q", idx, up)
	}

	down := statusBarView(40, false)
	if !strings.Contains(down, "Tx") {
		t.Errorf("expected status bar to contain \"Tx\", got %q", down)
	}
	if up == down {
		t.Errorf("expected up/down renders to differ (dot/text color), got identical output")
	}
}

// TestStatusBarViewShowsVersionOnRight confirms "v<appVersion>" sits at
// the right edge with the same margin gap the Tx side has on the left.
func TestStatusBarViewShowsVersionOnRight(t *testing.T) {
	prev := lipgloss.ColorProfile()
	lipgloss.SetColorProfile(termenv.TrueColor)
	defer lipgloss.SetColorProfile(prev)

	rendered := statusBarView(40, true)
	want := "v" + appVersion
	visible := stripANSI(rendered)
	if !strings.Contains(visible, want) {
		t.Fatalf("expected version tag %q in status bar, got %q", want, visible)
	}

	// Same margin width (len(statusBarMargin)) should trail the version
	// text before the visible line ends, mirroring the left side's gap
	// before the dot.
	trailing := visible[strings.Index(visible, want)+len(want):]
	if trailing != statusBarMargin {
		t.Errorf("expected exactly %q after the version tag, got %q", statusBarMargin, trailing)
	}
}

// TestAppModelTxStatusWiring confirms a txStatusMsg is consumed at the
// app level (updates m.txUp, reschedules the next check) rather than
// falling through to the per-screen broadcast path meant for
// WindowSizeMsg and key input.
func TestAppModelTxStatusWiring(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newAppModel(db)
	if m.txUp {
		t.Fatalf("expected txUp to start false before any check resolves")
	}

	updated, cmd := m.Update(txStatusMsg{up: true})
	m = updated.(appModel)
	if !m.txUp {
		t.Errorf("expected txStatusMsg{up: true} to set txUp")
	}
	if cmd == nil {
		t.Errorf("expected a rescheduled tx check command")
	}

	updated, _ = m.Update(txStatusMsg{up: false})
	m = updated.(appModel)
	if m.txUp {
		t.Errorf("expected txStatusMsg{up: false} to clear txUp")
	}
}

// TestAppModelCapturesWidthForStatusBar confirms the WindowSizeMsg width
// reaches appModel itself (so the status bar's background fills the full
// terminal width), not just the per-screen list.Model sizing.
func TestAppModelCapturesWidthForStatusBar(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newAppModel(db)
	updated, _ := m.Update(tea.WindowSizeMsg{Width: 123, Height: 40})
	m = updated.(appModel)

	if m.width != 123 {
		t.Errorf("expected appModel.width = 123, got %d", m.width)
	}
}

// stripANSI removes escape codes the way a terminal would display them,
// leaving only the visible characters.
func stripANSI(s string) string {
	var b strings.Builder
	inEscape := false
	for _, r := range s {
		if r == '\x1b' {
			inEscape = true
			continue
		}
		if inEscape {
			if r == 'm' {
				inEscape = false
			}
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

// lipglossVisibleWidth counts visible runes -- lipgloss.Width would also
// work, but this keeps the test's expectation independent of the
// library's own width function in case that's ever what's under test.
func lipglossVisibleWidth(s string) int {
	return len([]rune(stripANSI(s)))
}
