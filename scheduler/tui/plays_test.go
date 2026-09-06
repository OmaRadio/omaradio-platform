package main

import (
	"testing"

	"github.com/charmbracelet/bubbles/list"
	tea "github.com/charmbracelet/bubbletea"
)

// TestPlaysScreenRefreshWhileFiltered guards the same class of bug fixed
// on Music: reload() must return list.SetItems()'s tea.Cmd, or a filtered
// view goes permanently empty the moment "r" (or anything else calling
// reload) runs.
func TestPlaysScreenRefreshWhileFiltered(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	plays, err := listRecentPlays(db, recentPlaysLimit)
	if err != nil {
		t.Fatalf("listRecentPlays: %v", err)
	}
	if len(plays) == 0 {
		t.Fatalf("expected at least one recorded play")
	}

	m := newPlaysModel(db)
	m.list.SetFilterText(plays[0].Kind)

	if m.list.FilterState() == list.Unfiltered {
		t.Fatalf("expected SetFilterText to leave the list in a filtered state")
	}
	if len(m.list.VisibleItems()) == 0 {
		t.Fatalf("expected at least one visible item before refreshing")
	}

	updated, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("r")})
	m = updated

	if cmd == nil {
		t.Fatalf("expected a non-nil re-filter command after refreshing while filtered")
	}

	msg := cmd()
	updated, _ = m.Update(msg)
	m = updated

	if len(m.list.VisibleItems()) == 0 {
		t.Fatalf("bug reproduced: filtered list is empty after the reload command resolves")
	}
}

// TestPlaysScreenEnterClearsAppliedFilter mirrors the same Music-screen
// affordance: enter on an already-applied filter (not mid-type) clears it,
// since there's no per-row "select" action on a read-only reporting screen.
func TestPlaysScreenEnterClearsAppliedFilter(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	plays, err := listRecentPlays(db, recentPlaysLimit)
	if err != nil {
		t.Fatalf("listRecentPlays: %v", err)
	}
	if len(plays) == 0 {
		t.Fatalf("expected at least one recorded play")
	}

	m := newPlaysModel(db)
	fullCount := len(m.list.VisibleItems())

	m.list.SetFilterText(plays[0].Kind)
	if m.list.FilterState() != list.FilterApplied {
		t.Fatalf("expected FilterApplied after SetFilterText, got %v", m.list.FilterState())
	}

	updated, _ := m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = updated

	if m.list.FilterState() != list.Unfiltered {
		t.Errorf("expected enter to clear the applied filter, got state %v", m.list.FilterState())
	}
	if got := len(m.list.VisibleItems()); got != fullCount {
		t.Errorf("expected enter to restore all %d plays, got %d", fullCount, got)
	}
}
