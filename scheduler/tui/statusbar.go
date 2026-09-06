package main

import (
	"os/exec"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// appVersion is shown at the right of the status bar as "v<appVersion>" --
// bump by hand on release, there's no build-time injection for this
// single-binary, copy-to-deploy tool.
const appVersion = "1.0.0"

// txCheckInterval balances "feels live" against "don't spam systemctl" --
// the transmitter doesn't flap fast enough to need sub-10s resolution.
const txCheckInterval = 10 * time.Second

const styleStatusBarBGColor = "235" // really dark grey

var (
	styleStatusBarBG = lipgloss.NewStyle().Background(lipgloss.Color(styleStatusBarBGColor))
	// Text is the same hue as its dot, just darker -- 42/196 for the
	// dots, 29/124 for the text next to them.
	styleTxUpDot      = lipgloss.NewStyle().Background(lipgloss.Color(styleStatusBarBGColor)).Foreground(lipgloss.Color("42"))
	styleTxUpText     = lipgloss.NewStyle().Background(lipgloss.Color(styleStatusBarBGColor)).Foreground(lipgloss.Color("29"))
	styleTxDownDot    = lipgloss.NewStyle().Background(lipgloss.Color(styleStatusBarBGColor)).Foreground(lipgloss.Color("196"))
	styleTxDownText   = lipgloss.NewStyle().Background(lipgloss.Color(styleStatusBarBGColor)).Foreground(lipgloss.Color("124"))
	styleStatusBarVer = lipgloss.NewStyle().Background(lipgloss.Color(styleStatusBarBGColor)).Foreground(lipgloss.Color("245")) // neutral grey, doesn't compete with the tx status colors
)

// txStatusMsg reports whether cliamp-server (the actual streaming
// daemon, deploy/cliamp-server.service) is running. "Tx" here means
// literally the transmitter, matching how this whole platform already
// talks about itself (Transmitter-One), not a generic health check.
type txStatusMsg struct{ up bool }

// checkTxStatus shells out to systemctl rather than probing the stream
// port over HTTP -- this mirrors how a human operator (or every other
// script in this pipeline) already checks the service, needs no
// station-specific stream path, and never risks pulling audio bytes just
// to answer "is it up". Off the real transmitter host (no such unit, or
// no systemd at all -- e.g. this tool running against a dev-machine copy
// of the db) it fails closed to "down", which is the truthful answer:
// there's no real transmitter running here.
func checkTxStatus() tea.Msg {
	err := exec.Command("systemctl", "is-active", "--quiet", "cliamp-server").Run()
	return txStatusMsg{up: err == nil}
}

func scheduleTxCheck() tea.Cmd {
	return tea.Tick(txCheckInterval, func(time.Time) tea.Msg {
		return checkTxStatus()
	})
}

// statusBarMargin is rendered through styleStatusBarBG explicitly (not
// left as bare unstyled spaces) so it's guaranteed to carry the bar's
// background even though it sits inline next to other already-styled
// spans, rather than relying on lipgloss's own width-padding. Used on
// both ends: before the Tx dot on the left, after the version on the
// right, so the two mirror each other.
const statusBarMargin = "  "

// statusBarView renders a full-width, dark-grey bar: the Tx dot+label on
// the left, "v<appVersion>" on the right, and a plain-background fill
// between them so the whole line reads as one continuous bar rather than
// colored patches on the default terminal background.
func statusBarView(width int, txUp bool) string {
	dotStyle, textStyle := styleTxDownDot, styleTxDownText
	if txUp {
		dotStyle, textStyle = styleTxUpDot, styleTxUpText
	}
	left := styleStatusBarBG.Render(statusBarMargin) + dotStyle.Render("●") + textStyle.Render(" Tx")
	right := styleStatusBarVer.Render("v"+appVersion) + styleStatusBarBG.Render(statusBarMargin)

	if width <= 0 {
		return left + right
	}
	fillWidth := width - lipgloss.Width(left) - lipgloss.Width(right)
	if fillWidth < 0 {
		fillWidth = 0
	}
	fill := styleStatusBarBG.Render(strings.Repeat(" ", fillWidth))
	return left + fill + right
}
