# CollarCheck Chatbot & Search Backend Architecture

Welcome to the **CollarCheck Chatbot & Search Engine** backend documentation. This document provides a comprehensive technical overview of the application architecture, API routes, authentication keys, database schema, SQL joins, AI/LLM flow, data transfer budgets, hybrid search pipelines, multi-turn state concurrency, and overall system design.

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

### 🔄 Advanced Hybrid Search & Multi-Turn State Architecture

```
                      +------------------------------------------+
                      |          Web Frontend (HTML/JS)          |
                      |    (Action Controls, Playcards, Roadmap) |
                      +------------------------------------------+
                                           |
                                    HTTP / REST (JWT)
                                           v
                      +------------------------------------------+
                      |         FastAPI Unified Gateway          |
                      |       (api.py -> backend.main:app)       |
                      +------------------------------------------+
                        /          |             \             \
                       /           |              \             \
                      v            v               v             v
       +--------------------+ +-------------+ +-----------+ +---------------+
       | Hybrid Searcher    | | CAS Session | | MySQL DB  | | OpenAI / Groq |
       | (Meilisearch BM25  | | Concurrency | | (Users &  | | (LLM Reasoner |
       |  + Job Match API   | | State Store | |  Tenures) | |  & Intent)    |
       |  via RRF k=60)     | | (Optimistic)| |           | |               |
       +--------------------+ +-------------+ +-----------+ +---------------+
```

---

## 🔑 Environment Variables & API Keys

All operational credentials and configuration settings are defined in the [`.env`](file:///c:/UNG/chatbot_Quadrant/.env) file (see [`.env.example`](file:///c:/UNG/chatbot_Quadrant/.env.example) for template):

| Variable Name | Purpose | Default / Example Value |
| :--- | :--- | :--- |
| `SEARCH_API_KEY` | Bearer token required to access public search routes (`/search/*`). | Configured in `.env` |
| `MEILI_URL` | Base URL of the Meilisearch container instance. | `http://localhost:7701` |
| `MEILI_MASTER_KEY` | Administrative master key for Meilisearch operations. | `masterKey123` |
| `PLATFORM_TEST_TOKEN` / `EMPLOYEE_JWT` | Bearer JWT token used to call remote platform APIs. | JWT string |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | Connection details for the MySQL production relational database (`collarcheck`). | `localhost:3306`, `root`, `collarcheck` |
| `DB_PATH` | Local SQLite fallback / prototype database path. | `backend/collarcheck.db` |
| `OPENAI_API_KEY` / `GROQ_API_KEY` | API key for calling OpenAI GPT or Groq Llama models for chat completions. | `sk-...` / `gsk_...` |
| `JWT_SECRET` | Secret key used for signing and validating session JWTs. | `collarcheck_prototype_secret_key_123` |
| `JWT_ALGORITHM` | Algorithm used for JWT encoding/decoding. | `HS256` |
| `JOB_MATCH_API_BASE_URL` | Remote endpoint for CollarCheck Job Match neural/semantic retrieval. | `https://ai.collarcheck.com/jobmatch` |
| `JOB_MATCH_API_KEY` | Authentication key for Job Match API. | Configured in `.env` |
| `JOB_MATCH_ENABLED` | Toggle flag for enabling remote Job Match API retrieval. | `false` / `true` |
| `JOB_MATCH_TIMEOUT_SECONDS` | HTTP timeout for Job Match API requests. | `5.0` |
| `JOB_MATCH_LIMIT` | Max candidate/job records requested from Job Match API. | `11` |
| `HYBRID_SEARCH_ENABLED` | Master switch for Reciprocal Rank Fusion (RRF) search. | `true` |
| `HYBRID_MEILISEARCH_WEIGHT` | RRF fusion weight multiplier for Meilisearch results. | `0.70` |
| `HYBRID_JOBMATCH_WEIGHT` | RRF fusion weight multiplier for Job Match results. | `0.30` |
| `HYBRID_RRF_K` | Smoothing constant $k$ used in the RRF ranking formula. | `60` |
| `HYBRID_RETRIEVAL_LIMIT` | Maximum candidates/jobs to fetch before ranking. | `30` |
| `PLATFORM_MEDIA_BASE_URL` | Base domain for user avatars and company media. | `https://admin.collarcheck.com` |
| `ALLOWED_MEDIA_DOMAINS` | Whitelist of domains allowed for avatar/media rendering. | `admin.collarcheck.com,www.collarcheck.com,...` |

---

## 🔌 API Routes & Endpoints

### 1. Chat & Conversational AI Routes ([`backend/main.py`](file:///c:/UNG/chatbot_Quadrant/backend/main.py))

* **`POST /chat`**
  * **Function:** Main AI Chatbot entry point. Processes incoming chat messages, checks safety guardrails, updates user profile fields (if update intent is detected), fetches user profile context, queries relevant candidates or jobs via hybrid retrieval, calls OpenAI/Groq LLM, and returns formatted responses with interactive UI result cards and playcard actions.
  * **Auth Required:** Bearer JWT (`verify_token`).

* **`POST /reverse-geocode`**
  * **Function:** Converts GPS coordinates (`latitude`, `longitude`) into a location string (City, State, Country).

### 2. Search & Indexing Engine Routes ([`api.py`](file:///c:/UNG/chatbot_Quadrant/api.py) & [`backend/api/search.py`](file:///c:/UNG/chatbot_Quadrant/backend/api/search.py))

* **`GET /search/jobs`**
  * **Function:** Multi-tiered hybrid search for job listings with filtering by salary, skills, designation, department, industry, location, and role type.

* **`GET /search/users`**
  * **Function:** Candidate search engine allowing filters on verification status, skills, experience level, salary range, location, and availability (`on_immediate`, `on_notice`).

* **`GET /search/companies`**
  * **Function:** Search employer organizations by industry, location, and benefits.

* **`POST /rebuild-index`**
  * **Function:** Triggers asynchronous re-indexing of MySQL database records into Meilisearch indexes (`jobs`, `users`, `companies`).

* **`GET /health`**
  * **Function:** System health check verifying database connections, search engine status, and hybrid dependencies.

### 3. Profile Management Routes ([`backend/api/profile.py`](file:///c:/UNG/chatbot_Quadrant/backend/api/profile.py))

* **`GET /missing-fields`**: Inspects user profile completeness and returns missing fields.
* **`PUT /update`**: Updates basic profile fields (name, phone, description, gender, DOB).
* **`PUT /address`**: Updates present and permanent address lines.
* **`GET /skills` & `POST /skills`**: Retrieves or appends new user skills.
* **`GET /education` & `PUT /education`**: Retrieves or modifies user educational records.
* **`GET /employment` & `PUT /employment`**: Retrieves or modifies user work experience history.

### 4. Auth & Verification Routes ([`backend/api/auth.py`](file:///c:/UNG/chatbot_Quadrant/backend/api/auth.py), [`backend/api/employee.py`](file:///c:/UNG/chatbot_Quadrant/backend/api/employee.py), [`backend/api/employer.py`](file:///c:/UNG/chatbot_Quadrant/backend/api/employer.py))

* **`POST /auth/token`**: Generates and signs JWT tokens for authenticated platform sessions.
* **`GET /auth/verify`**: Validates incoming bearer tokens and decodes user role claims.
* **`GET /employee/verification-status`**: Retrieves multi-level verification state for employee records.
* **`POST /employee/verify`**: Submits verification evidence and triggers roadmap level updates.
* **`GET /employer/candidate-pool`**: Retrieves candidate recommendations tailored to employer parameters.

---

## 📊 Database Schema & SQL Joins

The system operates on **14+ core tables** in MySQL (indexed into Meilisearch by [`indexer.py`](file:///c:/UNG/chatbot_Quadrant/indexer.py)):

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
4. **Context Synthesis & In-Memory Caching ([`backend/services/user_data_service.py`](file:///c:/UNG/chatbot_Quadrant/backend/services/user_data_service.py))**:
   * Fetches user profile data and caches it for 15 minutes (`CACHE_TTL_SECONDS = 900`).
   * [`backend/services/user_data_extractor.py`](file:///c:/UNG/chatbot_Quadrant/backend/services/user_data_extractor.py) converts raw user profile fields into a structured text prompt block.
5. **RAG Search Retrieval**: Relevant job postings or candidate cards are retrieved via high-speed Meilisearch and Hybrid search queries.
6. **Prompt Assembly & LLM Call**: System prompt + User Profile Block + Search Context are packaged into a ~2,000–3,000 token payload sent to OpenAI or Groq.
7. **Security Verification**: Ensures candidate IDs returned in LLM output match authorized search results (`unauthorised = emitted - allowed_ids`).
8. **UI Delivery**: Returns JSON reply containing formatted response text, action controls, playcards, and candidate/job result cards to the frontend.

---

## 📈 Data Budget & Volume Breakdown

| Stage | Data Format | Estimated Size / Budget |
| :--- | :--- | :--- |
| **Incoming User Query** | JSON String | ~50 – 500 bytes |
| **Profile Context Injection** | Markdown / Text Block | ~1,000 – 1,500 tokens |
| **Meilisearch Overfetch Buffer** | JSON Array | Max 200 documents |
| **LLM Total Context Window** | Prompt + System + Context | ~2,500 – 4,000 tokens |
| **LLM Output Response** | Text / JSON Cards | ~300 – 800 tokens (~1 – 3 KB) |

---

## 🔀 Advanced Hybrid Search & Reciprocal Rank Fusion (RRF)

The search layer ([`backend/services/hybrid_job_search.py`](file:///c:/UNG/chatbot_Quadrant/backend/services/hybrid_job_search.py)) implements an enterprise-grade **Reciprocal Rank Fusion (RRF)** pipeline merging:

1. **Lexical/BM25 Keyword Retrieval**: Fast exact & typo-tolerant matches from Meilisearch.
2. **Semantic / Neural Job Match API**: Deep contextual vector rankings from the remote CollarCheck Job Match engine.

### RRF Ranking Formula:
$$RRF\_Score(d) = \sum_{m \in M} \frac{w_m}{k + r_m(d)}$$

Where:
- $M = \{\text{Meilisearch}, \text{JobMatch}\}$
- $k = 60$ (smoothing constant, configured via `HYBRID_RRF_K`)
- $w_{\text{Meili}} = 0.70$ (`HYBRID_MEILISEARCH_WEIGHT`)
- $w_{\text{JobMatch}} = 0.30$ (`HYBRID_JOBMATCH_WEIGHT`)
- $r_m(d)$ is the 1-based ordinal rank of document $d$ within retriever $m$

### Resilience & Fallbacks:
- If the remote Job Match API times out or is disabled, the system gracefully falls back to 100% Meilisearch retrieval without user interruption.
- Automatically handles document deduplication, score normalization, and provenance badge tagging (`Meilisearch Only`, `Job Match Only`, or `Hybrid Matched`).

---

## 🛡️ Multi-Turn Conversational Session State & Atomic CAS Concurrency

Multi-turn dialogues require tracking ongoing search filters, candidate lists, pending actions, and clarification intents. The state system ([`backend/utils/session_state.py`](file:///c:/UNG/chatbot_Quadrant/backend/utils/session_state.py)) provides:

- **Atomic Compare-And-Swap (CAS)**: Optimistic locking with monotonically incrementing `state_version` integers. If simultaneous asynchronous requests or rapid-fire user messages arrive, CAS prevents state overwrites or race conditions.
- **Context Retention Across Turns**:
  - Remembers active job mode (`Remote`, `On-site`, `Hybrid`), target salary, designations, and locations.
  - Maintains active candidate pagination tokens and filter refinements (*"Show only the ones in Pune"*, *"Clear the experience filter"*).
  - Preserves pending verification flows and multi-step dialog confirmations.
- **In-Memory TTL Cache with Database Hydration**: Fast sub-millisecond session access with automatic cleanup of expired sessions.

---

## 📋 Employment Verification Roadmap & Interactive Playcards

The conversational interface supports full end-to-end employment and candidate verification workflows:

### 3-Tier Verification Hierarchy:
1. **Tier 1 (Identity & Baseline)**: Confirms profile existence, contact details, and basic registration data.
2. **Tier 2 (Employment History & Tenures)**: Validates reported companies, job titles, start/end dates, and claimed company statuses.
3. **Tier 3 (Document & Reference Verification)**: Tracks formal documentation, payslips, manager referrals, and third-party verification integrity.

### Interactive Frontend Playcards ([`frontend/index.html`](file:///c:/UNG/chatbot_Quadrant/frontend/index.html)):
- **Action Control Chips**: Quick-tap action suggestions rendered below chatbot responses (e.g., `[Verify Employment]`, `[View Roadmap]`, `[Claim Company]`).
- **Employment Playcards**: Rich visual cards showing company name, tenure duration, verification badges, and missing information callouts.
- **Progress Roadmaps**: Visual step-by-step progress bars showing current verification milestones and next required actions.

---

## 🧠 Dynamic Skills Aggregation & Token Resolution

- **Dynamic Skills Resolution ([`backend/config/config.py`](file:///c:/UNG/chatbot_Quadrant/backend/config/config.py))**: `DynamicSkillsMap` dynamically inspects `cyb_company_job` and database tables at runtime for job profile skills, falling back seamlessly to default domain mappings if the database is offline.
- **User Skill Aggregator ([`backend/services/user_skill_aggregator.py`](file:///c:/UNG/chatbot_Quadrant/backend/services/user_skill_aggregator.py))**: Aggregates skills across profile declarations, past projects, and employment history with deduplication.
- **Token Resolver ([`backend/services/token_resolver.py`](file:///c:/UNG/chatbot_Quadrant/backend/services/token_resolver.py))**: Resolves ambiguous conversational tokens (e.g. *"React dev"*, *"Node engineer"*) into normalized database taxonomies for precise search execution.

---

## 📁 Repository Directory Structure

```
chatbot_Quadrant/
├── .env.example                     # Sample environment variable template
├── .gitignore                       # Git exclusion rules (venv, caches, scratch, results)
├── README.md                        # Project technical documentation
├── api.py                           # Unified entry point delegating to backend.main:app
├── docker-compose.yml               # Docker Compose file for Meilisearch container
├── indexer.py                       # Meilisearch database indexer script
├── requirements.txt                 # Python project dependencies
├── backend/
│   ├── main.py                      # FastAPI application core, routes, and chat handler
│   ├── collarcheck.db               # Local prototype database (ignored by git)
│   ├── api/                         # Modular route controllers
│   │   ├── auth.py                  # JWT authentication and verification routes
│   │   ├── employee.py              # Employee verification endpoints
│   │   ├── employer.py              # Employer candidate discovery routes
│   │   ├── profile.py               # Candidate profile management routes
│   │   └── search.py                # Modular search endpoints
│   ├── config/                      # Configuration & dynamic skill mappings
│   │   └── config.py                # Environment configs and DynamicSkillsMap
│   ├── recommendation/              # Candidate & job matching engines
│   │   ├── recommendation_engine.py # Core recommendation scoring
│   │   ├── skill_matcher.py         # Skill overlap calculators
│   │   ├── location_matcher.py      # Geographic proximity resolver
│   │   └── title_matcher.py         # Designation normalization matcher
│   ├── repositories/                # Database abstraction layers
│   │   ├── candidate_repository.py  # Candidate queries
│   │   ├── job_repository.py        # Job listings queries
│   │   └── people_repository.py     # People search queries
│   ├── schemas/                     # Pydantic data models & request/response contracts
│   │   ├── schemas.py               # Core API payload schemas
│   │   ├── search_criteria.py       # Extracted search filter representations
│   │   └── search_query.py          # Query input models
│   ├── services/                    # Business logic and external integrations
│   │   ├── hybrid_job_search.py     # Meilisearch + Job Match RRF fusion
│   │   ├── job_match_client.py      # HTTP client for CollarCheck Job Match API
│   │   ├── people_scoring_engine.py # Candidate scoring & relevance ranking
│   │   ├── token_resolver.py        # Entity token normalizer
│   │   ├── user_data_extractor.py   # User profile context extraction
│   │   ├── user_data_formatter.py   # LLM response and card formatting
│   │   ├── user_data_intent_parser.py # Regex + LLM intent classification
│   │   ├── user_data_service.py     # User data operations & caching
│   │   └── user_skill_aggregator.py # Multi-source skill consolidation
│   ├── utils/                       # Shared utility helpers
│   │   ├── session_state.py         # Atomic CAS session state manager
│   │   ├── location_resolver.py     # Geocoding & coordinate normalizer
│   │   └── utils.py                 # General helpers and sanitizers
│   └── tests/                       # Comprehensive regression and unit test suite
├── frontend/
│   └── index.html                   # Interactive web chat interface, playcards & roadmaps
└── tests/                           # Root integration tests
```

---

## 🚀 Quickstart & Local Setup

### 1. Prerequisites
- **Python**: Version 3.10+
- **Docker**: For running the local Meilisearch container
- **MySQL**: (Optional if using local SQLite fallback)

### 2. Environment Setup
```powershell
# Create and activate virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install project dependencies
pip install -r requirements.txt

# Copy environment configuration
cp .env.example .env
# Edit .env with your GROQ_API_KEY / OPENAI_API_KEY and credentials
```

### 3. Start Search Engine & Index Database
```powershell
# Launch Meilisearch via Docker
docker-compose up -d

# Index database records into Meilisearch
python indexer.py
```

### 4. Run Application Server
```powershell
# Run the unified FastAPI server
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```
Once started:
- **Web UI**: Open `frontend/index.html` in your browser.
- **Interactive API Docs (Swagger)**: Navigate to `http://localhost:8000/docs`.
- **Health Check**: `http://localhost:8000/health`.
