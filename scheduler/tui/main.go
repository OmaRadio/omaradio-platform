// Command tui is a terminal admin interface to OmaRadio's scheduler
// database (see ../db/migrations/). Meant to run directly on
// transmitter-one, against the real /opt/omaradio/db/omaradio.sqlite3 --
// not proxied over a network filesystem, for the same reason every
// Python script in this pipeline treats that DB as local-only: SQLite's
// locking model isn't safe over sshfs/NFS. `ssh` in and run this
// directly, same as the manual verification commands used throughout
// this project's own build.
//
// DB path resolution, same layered convention as the Python side's
// OMARADIO_DB_PATH: -db flag > OMARADIO_DB_PATH env var > the real
// production default. Unlike the Python scripts (whose fallback is a
// repo-relative dev path), this tool defaults straight to the
// production path, since it's meant to be run on transmitter-one first
// and foremost -- override with -db or OMARADIO_DB_PATH for local
// testing against a scratch copy.
package main

import (
	"flag"
	"fmt"
	"os"

	tea "github.com/charmbracelet/bubbletea"
)

const defaultDBPath = "/opt/omaradio/db/omaradio.sqlite3"

func resolveDBPath() string {
	dbFlag := flag.String("db", "", "Path to the scheduler sqlite db (default: $OMARADIO_DB_PATH, or "+defaultDBPath+")")
	flag.Parse()
	if *dbFlag != "" {
		return *dbFlag
	}
	if v := os.Getenv("OMARADIO_DB_PATH"); v != "" {
		return v
	}
	return defaultDBPath
}

func main() {
	dbPath := resolveDBPath()

	db, err := openDB(dbPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "opening db at %s: %v\n", dbPath, err)
		os.Exit(1)
	}
	defer db.Close()

	if _, err := db.Exec("SELECT 1"); err != nil {
		fmt.Fprintf(os.Stderr, "db at %s doesn't look right (run scheduler/db/migrate.py first?): %v\n", dbPath, err)
		os.Exit(1)
	}

	p := tea.NewProgram(newAppModel(db), tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "error running tui: %v\n", err)
		os.Exit(1)
	}
}
