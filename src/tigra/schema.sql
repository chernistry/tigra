CREATE TABLE IF NOT EXISTS person(id TEXT PRIMARY KEY, name TEXT NOT NULL, linkedin_url TEXT, email TEXT, headline TEXT, org_id TEXT);
CREATE TABLE IF NOT EXISTS org(id TEXT PRIMARY KEY, name TEXT NOT NULL, name_norm TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edge(id TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL, predicate TEXT NOT NULL, valid_from DATE, valid_to DATE, weight DOUBLE NOT NULL DEFAULT 1.0, class TEXT NOT NULL CHECK (class IN ('observed','inference')));
CREATE TABLE IF NOT EXISTS edge_source(edge_id TEXT NOT NULL, source TEXT NOT NULL, observed_at TIMESTAMP NOT NULL, method TEXT NOT NULL, confidence DOUBLE NOT NULL, derived_from TEXT);
ALTER TABLE edge_source ADD COLUMN IF NOT EXISTS note TEXT;
CREATE TABLE IF NOT EXISTS dm_event(person_id TEXT NOT NULL, conversation_id TEXT NOT NULL, sent_at TIMESTAMP NOT NULL, direction TEXT NOT NULL CHECK (direction IN ('out','in')));
