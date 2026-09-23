# CollarCheck Chatbot & Search Backend Architecture

Welcome to the **CollarCheck Chatbot & Search Engine** backend documentation. This document provides a comprehensive technical overview of the application architecture, API routes, authentication keys, database schema, SQL joins, AI/LLM flow, data transfer budgets, and overall system design.

---

## 🏗️ System Architecture Overview

```
                      +----------------------------------+
                      |       Web Frontend (HTML/JS)     |
                      +----------------------------------+
                                       |
                                HTTP / REST (JWT)
                                       v
                      +----------------------------------+
                      |     FastAPI Server (api.py)      |
                      |      & Backend (main.py)         |
                      +----------------------------------+
                         /             |              \
                        /              |               \
                       v               v                v
          +------------------+  +--------------+  +------------------+
          | Meilisearch Engine|  |  MySQL / DB  |  | OpenAI GPT Model |
          | (Search / RAG)   |  | (Relational) |  | (LLM Reasoning)  |
          +------------------+  +--------------+  +------------------+
```

The system combines a **FastAPI backend**, a **Meilisearch search engine** (for high-speed candidate, job, and company indexing), a **MySQL/SQLite database** (for relational persistent user data), and an **OpenAI LLM pipeline** (for natural language query parsing, intent detection, profile context synthesis, and conversational response generation).

---

## 🔑 Environment Variables & API Keys

All operational credentials and configuration settings are defined in the [`.env`](file:///c:/UNG/RM_CC_Chatgpt/chatbot%20v%28CORS%29%202/.env) file:

| Variable Name | Purpose | Default / Example Value |
| :--- | :--- | :--- |
| `SEARCH_API_KEY` | Bearer token required to access public search routes (`/search/*`). | Configured in `.env` |
| `MEILI_URL` | Base URL of the Meilisearch container instance. | `http://localhost:7701` |
| `MEILI_MASTER_KEY` | Administrative master key for Meilisearch operations. | `masterKey123` |
| `PLATFORM_TEST_TOKEN` / `EMPLOYEE_JWT` | Bearer JWT token used to call remote platform APIs. | JWT string |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | Connection details for the MySQL production relational database (`collarcheck`). | `localhost:3306`, `root`, `collarcheck` |
| `OPENAI_API_KEY` | Key for calling OpenAI GPT models for chat completions and profile updates. | `sk-...` |

---

## 🔌 API Routes & Endpoints

### 1. Chat & Conversational AI Routes ([`backend/main.py`](file:///c:/UNG/RM_CC_Chatgpt/chatbot%20v%28CORS%29%202/backend/main.py))

* **`POST /chat`**
  * **Function:** Main AI Chatbot entry point. Processes incoming chat messages, checks safety guardrails, updates user profile fields (if update intent is detected), fetches user profile context, queries relevant candidates or jobs, calls OpenAI LLM, and returns formatted responses with UI result cards.
  * **Auth Required:** Bearer JWT (`verify_token`).

* **`POST /reverse-geocode`**
  * **Function:** Converts GPS coordinates (`latitude`, `longitude`) into a location string (City, State, Country).

### 2. Search & Indexing Engine Routes ([`api.py`](file:///c:/UNG/RM_CC_Chatgpt/chatbot%20v%28CORS%29%202/api.py))

* **`GET /search/jobs`**
  * **Function:** Multi-tiered search for job listings with filtering by salary, skills, designation, department, industry, location, and role type.

* **`GET /search/users`**
  * **Function:** Candidate search engine allowing filters on verification status, skills, experience level, salary range, location, and availability (`on_immediate`, `on_notice`).

* **`GET /search/companies`**
  * **Function:** Search employer organizations by industry, location, and benefits.

* **`POST /rebuild-index`**
  * **Function:** Triggers asynchronous re-indexing of MySQL database records into Meilisearch indexes (`jobs`, `users`, `companies`).

* **`GET /health`**
  * **Function:** System health check verifying database connections and search engine status.

### 3. Profile Management Routes ([`backend/api/profile.py`](file:///c:/UNG/RM_CC_Chatgpt/chatbot%20v%28CORS%29%202/backend/api/profile.py))

* **`GET /missing-fields`**: Inspects user profile completeness and returns missing fields.
* **`PUT /update`**: Updates basic profile fields (name, phone, description, gender, DOB).
* **`PUT /address`**: Updates present and permanent address lines.
* **`GET /skills` & `POST /skills`**: Retrieves or appends new user skills.
* **`GET /education` & `PUT /education`**: Retrieves or modifies user educational records.
* **`GET /employment` & `PUT /employment`**: Retrieves or modifies user work experience history.

---

## 📊 Database Schema & SQL Joins

The system operates on **14+ core tables** in MySQL (indexed into Meilisearch by [`indexer.py`](file:///c:/UNG/RM_CC_Chatgpt/chatbot%20v%28CORS%29%202/indexer.py)):

### Primary Data Tables:
1. **`cyb_user`**: Primary user table (candidates, employees, company accounts). Stores contact info, location IDs, verification flags, and profile summaries.
2. **`cyb_company_job`**: Job postings master table storing title, description, department, designation, salary ranges, and job location.
3. **`cyb_user_skill` & `cyb_skill`**: Relational junction table linking user IDs to master skill entries.
4. **`cyb_user_education`**: Stores university, course, course type (full-time vs online), start/end dates, and degree level.
5. **`cyb_user_experience`**: Stores employment history, company, designation, salary, and employment dates.
6. **`cyb_application`**: Job application logs per candidate and job posting.
7. **`cyb_company_benefits`**: Benefits offered by employer organizations.
8. **`cyb_country`**, **`cyb_state`**, **`cyb_cities`**: Location master tables.
9. **`cyb_department`**, **`cyb_designation`**, **`cyb_industries`**, **`cyb_role_types`**: Domain taxonomy tables.
10. **`designation_master` / `designation_alias` & `company_master` / `company_alias`**: Semantic normalization mapping tables for job titles and company names.

### Key SQL Joins & Relationships

During search indexing and context retrieval, the following SQL join strategy is executed:

```sql
-- Example Candidates & Experience Indexing Query (indexer.py)
SELECT 
    u.id, u.full_name, u.email, u.phone,
    cntry.name AS country_name, st.name AS state_name, ct.name AS city_name,
    desig.name AS current_position_name,
    GROUP_CONCAT(DISTINCT s.name SEPARATOR '||') AS skill_names_csv
FROM cyb_user u
LEFT JOIN cyb_country cntry      ON cntry.id = u.country
LEFT JOIN cyb_state st           ON st.id = u.state
LEFT JOIN cyb_cities ct          ON ct.id = u.city
LEFT JOIN cyb_designation desig  ON desig.id = u.current_possition
LEFT JOIN cyb_user_skill us      ON us.user = u.id AND us.status = 1 AND us.is_deleted = 0
LEFT JOIN cyb_skill s           ON s.id = us.skill
WHERE u.status = 1 AND u.is_deleted = 0
GROUP BY u.id;
```

---

## 🤖 AI Execution Flow & Data Pipeline

```
[User Message] ──> [Guardrails Check] ──> [Profile Update Intent Parser]
                                                     │
[Meilisearch / DB RAG] <── [Profile Context Loader] <┘ (Reads 15-min TTL Cache)
          │
          v
[System Prompt Assembly] ──> [OpenAI GPT Completion] ──> [ID Security Audit] ──> [JSON Response to UI]
```

### Step-by-Step Data Journey:

1. **Request Ingestion (`POST /chat`)**: Frontend sends user query payload (~100–500 bytes).
2. **Safety Guardrails**: Fast regex scanner checks for non-permitted or out-of-scope prompts.
3. **Intent Detection & DB Auto-Update**: If the message contains profile edits (e.g., *"I updated my skill to React"*), `parse_profile_update()` extracts the entity and updates `cyb_user_skill` / `cyb_user` directly in SQL.
4. **Context Synthesis & In-Memory Caching ([`src/profile/loader.py`](file:///c:/UNG/RM_CC_Chatgpt/chatbot%20v%28CORS%29%202/src/profile/loader.py))**:
   * Fetches user profile data and caches it for 15 minutes (`CACHE_TTL_SECONDS = 900`).
   * [`src/profile/context.py`](file:///c:/UNG/RM_CC_Chatgpt/chatbot%20v%28CORS%29%202/src/profile/context.py) converts raw user profile fields into a structured text prompt block.
5. **RAG Search Retrieval**: Relevant job postings or candidate cards are retrieved via high-speed Meilisearch queries.
6. **Prompt Assembly & LLM Call**: System prompt + User Profile Block + Search Context are packaged into a ~2,000–3,000 token payload sent to OpenAI.
7. **Security Verification**: Ensures candidate IDs returned in LLM output match authorized search results (`unauthorised = emitted - allowed_ids`).
8. **UI Delivery**: Returns JSON reply containing formatted response text and candidate/job result cards to the frontend.

---

## 📈 Data Budget & Volume Breakdown

| Stage | Data Format | Estimated Size / Budget |
| :--- | :--- | :--- |
| **Incoming User Query** | JSON String | ~50 – 500 bytes |
| **Profile Context Injection** | Markdown / Text Block | ~1,000 – 1,500 tokens |
| **Meilisearch Overfetch Buffer** | JSON Array | Max 200 documents |
| **LLM Total Context Window** | Prompt + System + Context | ~2,500 – 4,000 tokens |
| **LLM Output Response** | Text / JSON Cards | ~300 – 800 tokens (~1 – 3 KB) |
