-- CC-Chatbot DB Schema Migration Script (MySQL / SQLite Compatible)
-- Phase 2: Data Normalization Layer & Alias Tables

-- 1. Add Canonical Columns to Existing Tables (Backward Compatible)
-- Note: In SQLite, "ADD COLUMN" is supported. In MySQL, these will append cleanly.

ALTER TABLE cyb_company_job ADD COLUMN canonical_designation VARCHAR(255) DEFAULT NULL;
ALTER TABLE cyb_company_job ADD COLUMN canonical_company VARCHAR(255) DEFAULT NULL;

-- If cyb_user_experience table exists in the environment:
-- ALTER TABLE cyb_user_experience ADD COLUMN canonical_designation VARCHAR(255) DEFAULT NULL;
-- ALTER TABLE cyb_user_experience ADD COLUMN canonical_company VARCHAR(255) DEFAULT NULL;


-- 2. Create Designation Master & Alias Tables for Semantic Mapping

CREATE TABLE IF NOT EXISTS designation_master (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canonical_name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS designation_alias (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canonical_id INT NOT NULL,
    alias VARCHAR(255) NOT NULL UNIQUE,
    FOREIGN KEY (canonical_id) REFERENCES designation_master(id) ON DELETE CASCADE
);


-- 3. Create Company Master & Alias Tables for Semantic Mapping

CREATE TABLE IF NOT EXISTS company_master (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canonical_name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS company_alias (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canonical_id INT NOT NULL,
    alias VARCHAR(255) NOT NULL UNIQUE,
    FOREIGN KEY (canonical_id) REFERENCES company_master(id) ON DELETE CASCADE
);


-- 4. Initial Seed Data Example (Optional/Reference)
-- INSERT INTO designation_master (canonical_name) VALUES ('Backend Developer');
-- INSERT INTO designation_alias (canonical_id, alias) VALUES (1, 'backend engineer'), (1, 'backend programmer'), (1, 'server-side developer');


-- 5. Phase 5: Multi-Turn Session State Version Sequence
ALTER TABLE cyb_chat_session ADD COLUMN version_seq INT NOT NULL DEFAULT 1;

