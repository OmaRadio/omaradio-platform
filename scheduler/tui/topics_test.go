package main

import (
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// TestInitialModelSurvivesStartupResize is a regression test for a real
// crash: bubbletea sends a WindowSizeMsg immediately on startup, before
// the user has ever pressed "a" to open the add form. Update()'s
// WindowSizeMsg handler unconditionally resizes m.form.summaryInput,
// which panicked when m.form was still the zero-value addForm{} (a
// textarea.Model that never went through textarea.New()). Confirmed for
// real via manual testing before the fix (newTopicsModel now constructs a
// real newAddForm() up front).
func TestInitialModelSurvivesStartupResize(t *testing.T) {
	db, err := openDB(testDBPath(t))
	if err != nil {
		t.Fatalf("openDB: %v", err)
	}
	defer db.Close()

	m := newTopicsModel(db)

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("Update() panicked on startup WindowSizeMsg: %v", r)
		}
	}()
	_, _ = m.Update(tea.WindowSizeMsg{Width: 120, Height: 40})
}
