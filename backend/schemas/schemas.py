from pydantic import BaseModel
from typing import Optional, List, Any, Union, Dict

class LoginRequest(BaseModel):
    email: str
    role: str

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    resume_context: Optional[str] = None
    user_type: Optional[str] = "employee"
    session_id: Optional[str] = "default_session"
    user_id: Optional[str] = None
    user_location: Optional[Union[str, Dict[str, Any]]] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    user_detail: Optional[Dict[str, Any]] = None
    widgets_data: Optional[Any] = None

class GeocodeRequest(BaseModel):
    latitude: float
    longitude: float

class GeocodeResponse(BaseModel):
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    location_str: str
    success: bool = True

class JobCard(BaseModel):
    job_id: int
    title: str
    company: Optional[str] = None
    department: Optional[str] = None      # new
    location: Optional[str] = None
    job_mode: Optional[str] = None
    experience: Optional[str] = None
    vacancy: Optional[int] = None         # new
    salary: Optional[str] = None
    preview: Optional[str] = None         # new
    description: Optional[str] = None     # full description for read-more
    url: str
    match_reason: Optional[str] = None
    global_rank: Optional[int] = None
    match_score: Optional[float] = None
    matched_skills: Optional[List[str]] = None
    recommendation_explanation: Optional[str] = None
    recommendation_reasons: Optional[List[Dict[str, Any]]] = None


class CandidateCard(BaseModel):
    cc_id: str
    name: str
    position: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    experience_label: Optional[str] = None
    skills: List[str] = []
    rating: Optional[float] = None
    url: str
    match_reason: Optional[str] = None

class OrganizationCard(BaseModel):
    org_id: Optional[int] = None
    name: str
    industry: Optional[str] = None
    company_size: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    location: Optional[str] = None
    distance: Optional[float] = None
    distance_label: Optional[str] = None
    match_reason: Optional[str] = None
    url: Optional[str] = None

class EducationCard(BaseModel):
    id: Optional[int] = None
    qualification: str
    institution: str
    course_type: Optional[str] = "Full Time"
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_highest: Optional[bool] = False
    url: Optional[str] = "https://www.collarcheck.com/dashboard/user/education"
    match_reason: Optional[str] = None

class ChatResponse(BaseModel):
    reply: str
    success: bool
    request_id: Optional[str] = None
    results: Optional[List[Union[JobCard, CandidateCard, OrganizationCard, EducationCard, Dict[str, Any], Any]]] = None
    result_type: Optional[str] = None
    jobs: Optional[List[JobCard]] = None
    candidates: Optional[List[CandidateCard]] = None
    result_total: Optional[int] = None
    pagination: Optional[Dict[str, Any]] = None
    suggestion_chips: Optional[List[Any]] = None


class ProfileUpdateRequest(BaseModel):
    fname: Optional[str] = None
    lname: Optional[str] = None
    phone: Optional[str] = None
    gender: Optional[Any] = None
    profile_description: Optional[str] = None
    dob: Optional[str] = None
    expected_salary: Optional[Any] = None

    city: Optional[int] = None
    state: Optional[int] = None
    country: Optional[int] = None
    address: Optional[str] = None
    street_address: Optional[str] = None
    present_address: Optional[str] = None
    permanent_address: Optional[str] = None
    same_address: Optional[int] = None

class ProfileMissingFieldsResponse(BaseModel):
    user_id: int
    email: str
    missing_fields: List[str]
    profile_complete: bool

class AddressUpdateRequest(BaseModel):
    address: Optional[str] = None
    street_address: Optional[str] = None
    present_address: Optional[str] = None
    permanent_address: Optional[str] = None
    same_address: Optional[int] = None
    address_type: Optional[str] = None  # "present", "permanent", "both"
    city: Optional[Any] = None
    state: Optional[Any] = None
    country: Optional[Any] = None

class SkillAddRequest(BaseModel):
    skills: List[Any]  # Can be list of skill names (str) or skill IDs (int)
    rating: Optional[int] = 5

class SkillItem(BaseModel):
    skill_id: int
    skill_name: str
    rating: Optional[int] = 5

class EducationAddRequest(BaseModel):
    university: Optional[Any] = None
    course: Optional[Any] = None
    course_type: Optional[Any] = 1
    country: Optional[Any] = None
    state: Optional[Any] = None
    city: Optional[Any] = None
    ishighest: Optional[Any] = 0
    starting_date: Optional[str] = None
    ending_date: Optional[str] = None
    ongoing: Optional[int] = 0

class EmploymentAddRequest(BaseModel):
    company: Optional[Any] = None
    designation: Optional[Any] = None
    department: Optional[Any] = None
    employment_type: Optional[Any] = 1
    joining_date: Optional[str] = None
    worked_till_date: Optional[str] = None
    still_working: Optional[int] = 0
    hired: Optional[int] = 0
    description: Optional[str] = None
    salary: Optional[Any] = None
    salary_inhand: Optional[str] = "CTC"
    salary_mode: Optional[str] = "Annually"
    skill: Optional[List[Any]] = None




