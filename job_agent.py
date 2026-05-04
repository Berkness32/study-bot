"""
job_agent.py — Job Application Agent
Pauses at each step for user approval before proceeding.

Usage:
    python job_agent.py --url "https://example.com/jobs/123"
    python job_agent.py --url "https://example.com/jobs/123" --headless

Requirements:
    pip install playwright python-docx pyyaml ollama
    playwright install chromium
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import ollama
import yaml
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────
COMPONENTS_PATH     = Path("data/job-apps/components.yaml")
OUTPUT_DIR          = Path("data/job-apps/output")
ACTIONS_LOG         = Path("logs/actions_log.json")
CHAT_MODEL          = "qwen3:8b"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────

def _strip_llm_raw(text: str) -> str:
    text = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```\s*',     '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```$',     '', text)
    return text.strip()


def _add_hyperlink(para, url: str, text: str, size_pt: float = 11):
    """Append a blue underlined hyperlink run to an existing paragraph."""
    part = para.part
    r_id = part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)
    run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    color = OxmlElement('w:color')
    color.set(qn('w:val'), '0563C1')
    rPr.append(color)
    u = OxmlElement('w:u')
    u.set(qn('w:val'), 'single')
    rPr.append(u)
    sz = OxmlElement('w:sz')
    sz.set(qn('w:val'), str(int(size_pt * 2)))
    rPr.append(sz)
    run.append(rPr)
    t = OxmlElement('w:t')
    t.text = text
    run.append(t)
    hyperlink.append(run)
    para._p.append(hyperlink)


def log_action(action: str, job_title: str, company: str,
               outcome: str, notes: str = ""):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "action":    action,
        "job_title": job_title,
        "company":   company,
        "outcome":   outcome,
        "notes":     notes,
    }
    entries = []
    if ACTIONS_LOG.exists():
        with open(ACTIONS_LOG, encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                entries = []
    entries.append(entry)
    with open(ACTIONS_LOG, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"  📋 Logged: {action} → {outcome}")


# ── User approval ─────────────────────────────────────────────────────────────

def ask_approval(prompt: str, details: str = "") -> bool:
    print()
    print("=" * 60)
    print(f"⏸  APPROVAL REQUIRED: {prompt}")
    if details:
        print()
        print(details)
    print("=" * 60)
    while True:
        answer = input("  Proceed? [y/n/q to quit]: ").strip().lower()
        if answer == "y":
            return True
        elif answer == "n":
            return False
        elif answer == "q":
            print("Exiting.")
            sys.exit(0)
        else:
            print("  Please enter y, n, or q.")


# ── Components loader ─────────────────────────────────────────────────────────

def load_components() -> dict:
    if not COMPONENTS_PATH.exists():
        print(f"ERROR: components.yaml not found at {COMPONENTS_PATH}")
        sys.exit(1)
    with open(COMPONENTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Step 1: Read job description ──────────────────────────────────────────────

def read_job_description(url: str, headless: bool = False) -> dict:
    print(f"\n{'='*60}")
    print("STEP 1 — Reading job description")
    print(f"  URL: {url}")
    print(f"{'='*60}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page    = browser.new_page()
        print("  Opening browser...")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        full_text = page.inner_text("body")
        title_tag = page.title()
        browser.close()

    print("  Extracting job details with LLM...")
    prompt = f"""Extract the following from this job posting text.
Return ONLY a JSON object with keys: job_title, company, summary, requirements, responsibilities.
Keep each value concise (under 300 words each).

Job posting text:
{full_text[:6000]}

Return only valid JSON, no markdown, no explanation."""

    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    raw = _strip_llm_raw(response["message"]["content"])

    try:
        job_info = json.loads(raw)
    except json.JSONDecodeError:
        job_info = {
            "job_title":        title_tag,
            "company":          "Unknown",
            "summary":          full_text[:500],
            "requirements":     "",
            "responsibilities": "",
        }

    job_info["url"]       = url
    job_info["full_text"] = full_text[:8000]

    # Capture the actual apply URL (may redirect to ATS like Greenhouse)
    with sync_playwright() as p2:
        browser2 = p2.chromium.launch(headless=headless)
        page2    = browser2.new_page()
        page2.goto(url, wait_until="domcontentloaded", timeout=30000)
        page2.wait_for_timeout(2000)
        apply_url = page2.evaluate("""() => {
            const links = [...document.querySelectorAll('a')];
            const applyLink = links.find(a => /apply/i.test(a.innerText) && a.href && !a.href.includes('builtin'));
            return applyLink ? applyLink.href : null;
        }""")
        browser2.close()

    job_info["apply_url"] = apply_url or url
    print(f"\n  Job Title : {job_info.get('job_title', 'N/A')}")
    print(f"  Company   : {job_info.get('company', 'N/A')}")
    print(f"  Apply URL : {job_info['apply_url']}")
    print(f"\n  Summary   : {job_info.get('summary', '')[:300]}...")
    return job_info


# ── Step 2: Select components ─────────────────────────────────────────────────

def select_components(job_info: dict, components: dict) -> dict:
    """
    Use LLM to select bullet points and ordering — but ALL experience
    and ALL projects are always included. Agent only chooses which bullets
    to highlight and which skills to list first.
    """
    print(f"\n{'='*60}")
    print("STEP 2 — Selecting relevant components")
    print(f"{'='*60}")

    # Tell the LLM exactly which keys exist — prevents hallucination
    available_skill_keys = list(components.get("skills", {}).keys())
    # Build human-readable skill descriptions for the prompt
    skill_descriptions = {
        k: v.get("value", "")[:60]
        for k, v in components.get("skills", {}).items()
    }
    all_project_titles   = [p["title"] for p in components.get("projects", [])]
    all_experience       = components.get("experience", [])
    cover_para_keys      = list(components.get("cover_letter_paragraphs", {}).keys())

    # Build bullet options per company for the prompt
    exp_options = {}
    for exp in all_experience:
        exp_options[exp["company"]] = {
            "title":   exp["title"],
            "dates":   exp["dates"],
            "bullets": [b["text"] for b in exp.get("bullets", [])],
        }

    prompt = f"""You are a resume tailoring assistant.

JOB DESCRIPTION:
Title: {job_info.get('job_title')}
Company: {job_info.get('company')}
Requirements: {job_info.get('requirements', '')}
Responsibilities: {job_info.get('responsibilities', '')}

YOUR TASK:
1. Select which skills to include (choose from EXACTLY these keys, no others):
   {json.dumps(skill_descriptions, indent=2)}

2. For each work experience below, select EXACTLY 3 bullet points to include.
   You MUST include ALL companies — do not skip any.
   If a company has fewer than 3 bullets available, include all of them.
   {json.dumps(exp_options, indent=2)}

3. Select which projects to include (you MUST include all of them):
   {all_project_titles}

4. Select cover letter paragraph keys in order (choose from EXACTLY these keys):
   {cover_para_keys}

5. List 5-10 keywords from the job description to naturally mirror.

6. List anything the job requires that is NOT available in the skills/experience above.

Return ONLY a JSON object with these exact keys:
- selected_skills: list of skill key names (from the available list only)
- selected_experience: list of objects with "company", "title", "dates", "selected_bullets" (list of bullet text strings)
- selected_projects: list of all project titles
- selected_cover_paragraphs: list of paragraph key names
- keywords_to_mirror: list of keywords
- flagged_fields: list of missing requirements

Return only valid JSON, no markdown."""

    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    raw = _strip_llm_raw(response["message"]["content"])

    try:
        selected = json.loads(raw)
    except json.JSONDecodeError:
        print("  WARNING: Could not parse LLM selection — using all components as fallback")
        selected = {
            "selected_skills": available_skill_keys,
            "selected_experience": [
                {
                    "company":          e["company"],
                    "title":            e["title"],
                    "dates":            e["dates"],
                    "selected_bullets": [b["text"] for b in e.get("bullets", [])[:3]],
                }
                for e in all_experience
            ],
            "selected_projects":        all_project_titles,
            "selected_cover_paragraphs": ["opening_swe", "cs_education", "technology_aide", "closing"],
            "keywords_to_mirror":       [],
            "flagged_fields":           [],
        }

   # ── Safety net: ensure ALL experience and projects are always included ────
    selected_companies = {e["company"] for e in selected.get("selected_experience", [])}
    for exp in all_experience:
        if exp["company"] not in selected_companies:
            selected.setdefault("selected_experience", []).append({
                "company":          exp["company"],
                "title":            exp["title"],
                "dates":            exp["dates"],
                "selected_bullets": [b["text"] for b in exp.get("bullets", [])[:3]],
            })

    # Pad any experience that has fewer than 3 bullets
    exp_bullet_lookup = {e["company"]: e for e in all_experience}
    for sel_exp in selected["selected_experience"]:
        company = sel_exp["company"]
        current_bullets = sel_exp.get("selected_bullets", [])
        if len(current_bullets) < 3 and company in exp_bullet_lookup:
            all_bullets = [b["text"] for b in exp_bullet_lookup[company].get("bullets", [])]
            for b in all_bullets:
                if b not in current_bullets:
                    current_bullets.append(b)
                if len(current_bullets) >= 3:
                    break
        sel_exp["selected_bullets"] = current_bullets

    selected_proj_titles = set(selected.get("selected_projects", []))
    for proj in components.get("projects", []):
        if proj["title"] not in selected_proj_titles:
            selected.setdefault("selected_projects", []).append(proj["title"])

    # ── Filter skill keys to only valid ones ──────────────────────────────────
    valid_skills = [k for k in selected.get("selected_skills", [])
                    if k in available_skill_keys]
    selected["selected_skills"] = valid_skills if valid_skills else available_skill_keys[:4]

    print(f"\n  Skills selected     : {selected.get('selected_skills')}")
    print(f"  Experience included : {[e['company'] for e in selected.get('selected_experience', [])]}")
    print(f"  Projects included   : {selected.get('selected_projects')}")
    print(f"  Cover paragraphs    : {selected.get('selected_cover_paragraphs')}")
    print(f"  Keywords to mirror  : {selected.get('keywords_to_mirror')}")
    if selected.get("flagged_fields"):
        print(f"\n  ⚠️  FLAGGED (not in library): {selected['flagged_fields']}")

    return selected


# ── Step 3: Generate documents ────────────────────────────────────────────────

def _add_section_header(doc, text: str):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    p.paragraph_format.space_after  = Pt(2)
    run = p.add_run(text)
    run.bold = True
    run.underline = True
    run.font.size = Pt(12)


def _add_bullet(doc, text: str):
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after  = Pt(1)
    p.add_run(text).font.size = Pt(11)


def build_resume_docx(job_info: dict, selected: dict,
                      components: dict, output_path: Path):
    doc = Document()

    for section in doc.sections:
        section.top_margin    = Inches(0.75)
        section.bottom_margin = Inches(0.75)
        section.left_margin   = Inches(0.75)
        section.right_margin  = Inches(0.75)

    p_info = components["personal"]

    # ── Header ────────────────────────────────────────────────────────────────
    name_p = doc.add_paragraph()
    name_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name_p.paragraph_format.space_before = Pt(0)
    name_p.paragraph_format.space_after  = Pt(2)
    run = name_p.add_run(p_info["name"])
    run.bold = True
    run.font.size = Pt(16)

    contact_p = doc.add_paragraph()
    contact_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact_p.paragraph_format.space_before = Pt(0)
    contact_p.paragraph_format.space_after  = Pt(1)
    contact_p.add_run(
        f"{p_info['location']} | {p_info['phone']} | {p_info['email']}"
    ).font.size = Pt(10)

    links_p = doc.add_paragraph()
    links_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    links_p.paragraph_format.space_before = Pt(0)
    links_p.paragraph_format.space_after  = Pt(10)
    _add_hyperlink(links_p, p_info["portfolio"], p_info["portfolio"], size_pt=10)
    links_p.add_run(" | ").font.size = Pt(10)
    _add_hyperlink(links_p, p_info["linkedin"],  p_info["linkedin"],  size_pt=10)

    # ── Skills & Qualifications ───────────────────────────────────────────────
    _add_section_header(doc, "Skills & Qualifications:")
    skills = components.get("skills", {})
    label_map = {
        "coding_languages":            "Coding Languages",
        "software_engineer_utilities": "Software & Utilities",
        "software_utilities":          "Software & Utilities",
        "backend":                     "Backend",
        "networking":                  "Networking",
        "soft_skills":                 "Soft Skills",
        "math_courses":                "Math Courses",
        "program_event_operations":    "Program & Event Operations",
        "administrative_facility":     "Administrative & Facility Management",
        "health_safety":               "Health, Safety & Compliance",
        "technology_data":             "Technology & Data Tracking",
        "interpersonal_leadership":    "Interpersonal & Leadership Skills",
    }
    for key in selected.get("selected_skills", []):
        if key in skills:
            label = label_map.get(key, key.replace("_", " ").title())
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after  = Pt(1)
            bold_run = p.add_run(label)
            bold_run.bold = True
            bold_run.font.size = Pt(11)
            p.add_run(f": {skills[key]['value']}").font.size = Pt(11)

    # ── Certifications ────────────────────────────────────────────────────────
    _add_section_header(doc, "Certifications:")
    for cert in components.get("certifications", []):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after  = Pt(1)
        bold_run = p.add_run(cert["name"])
        bold_run.bold = True
        bold_run.font.size = Pt(11)
        if cert.get("expires"):
            detail = f". Issued by: {cert['issuer']}. Expires: {cert['expires']}."
        else:
            detail = f" ({cert.get('status', 'In Progress')}). Expected: {cert.get('expected', '')}."
        p.add_run(detail).font.size = Pt(11)

    # ── Projects ──────────────────────────────────────────────────────────────
    _add_section_header(doc, "Projects:")
    proj_lookup = {proj["title"]: proj for proj in components.get("projects", [])}
    for proj_title in selected.get("selected_projects", []):
        if proj_title not in proj_lookup:
            continue
        proj = proj_lookup[proj_title]
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after  = Pt(1)
        bold_run = p.add_run(proj["role"])
        bold_run.bold = True
        bold_run.font.size = Pt(11)
        p.add_run(f". {proj['title']}. {proj['dates']}.").font.size = Pt(11)
        for b in proj.get("bullets", []):
            _add_bullet(doc, b["text"])
        for link_url in (proj.get("links") or {}).values():
            lp = doc.add_paragraph()
            lp.paragraph_format.left_indent  = Inches(0.25)
            lp.paragraph_format.space_before = Pt(0)
            lp.paragraph_format.space_after  = Pt(1)
            _add_hyperlink(lp, link_url, link_url, size_pt=10)

    # ── Education ─────────────────────────────────────────────────────────────
    _add_section_header(doc, "Education:")
    for edu in components.get("education", []):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after  = Pt(0)
        run = p.add_run(edu["institution"])
        run.bold = True
        run.font.size = Pt(11)
        p2 = doc.add_paragraph()
        p2.paragraph_format.space_before = Pt(0)
        p2.paragraph_format.space_after  = Pt(0)
        r2 = p2.add_run(edu["dates"])
        r2.italic = True
        r2.font.size = Pt(11)
        p3 = doc.add_paragraph()
        p3.paragraph_format.space_before = Pt(0)
        p3.paragraph_format.space_after  = Pt(1)
        r3 = p3.add_run(edu["degree"])
        r3.italic = True
        r3.font.size = Pt(11)
        if edu.get("courses"):
            kcp = doc.add_paragraph()
            kcp.paragraph_format.space_before = Pt(0)
            kcp.paragraph_format.space_after  = Pt(1)
            kcp.add_run("Key Courses:").font.size = Pt(11)
            for course in edu["courses"]:
                if " - http" in course:
                    name, url = course.split(" - ", 1)
                    bp = doc.add_paragraph(style='List Bullet')
                    bp.paragraph_format.space_before = Pt(0)
                    bp.paragraph_format.space_after  = Pt(1)
                    bp.add_run(name + " — ").font.size = Pt(11)
                    _add_hyperlink(bp, url, url, size_pt=11)
                else:
                    _add_bullet(doc, course)

    # ── Experience — split into Related and Additional ─────────────────────────
    RELATED_TAGS = {"it", "tech", "software_engineer", "networking"}
    exp_lookup   = {e["company"]: e for e in components.get("experience", [])}

    def _is_related(company: str) -> bool:
        for b in exp_lookup.get(company, {}).get("bullets", []):
            if set(b.get("tags", [])) & RELATED_TAGS:
                return True
        return False

    def _write_exp_block(sel_exp: dict):
        p1 = doc.add_paragraph()
        p1.paragraph_format.space_before = Pt(8)
        p1.paragraph_format.space_after  = Pt(0)
        r1 = p1.add_run(sel_exp["title"])
        r1.bold = True
        r1.font.size = Pt(11)
        p2 = doc.add_paragraph()
        p2.paragraph_format.space_before = Pt(0)
        p2.paragraph_format.space_after  = Pt(0)
        r2 = p2.add_run(sel_exp["company"])
        r2.bold = True
        r2.font.size = Pt(11)
        p3 = doc.add_paragraph()
        p3.paragraph_format.space_before = Pt(0)
        p3.paragraph_format.space_after  = Pt(1)
        p3.add_run(sel_exp["dates"]).font.size = Pt(11)
        for bullet in sel_exp.get("selected_bullets", []):
            _add_bullet(doc, bullet)

    selected_exp   = selected.get("selected_experience", [])
    related_exp    = [e for e in selected_exp if _is_related(e["company"])]
    additional_exp = [e for e in selected_exp if not _is_related(e["company"])]

    if related_exp:
        _add_section_header(doc, "Related Experience")
        for sel_exp in related_exp:
            _write_exp_block(sel_exp)

    if additional_exp:
        _add_section_header(doc, "Additional Experience:")
        for sel_exp in additional_exp[:2]:
            _write_exp_block(sel_exp)

    doc.save(str(output_path))


def generate_cover_letter_text(job_info: dict, selected: dict, components: dict) -> list[str]:
    """Use LLM to write 3 tailored body paragraphs for the cover letter."""
    skills_lines = [
        f"{k.replace('_', ' ').title()}: {components['skills'][k]['value'][:80]}"
        for k in selected.get("selected_skills", [])
        if k in components.get("skills", {})
    ]
    exp_summary = "\n".join(
        f"- {e['title']} at {e['company']}: " + "; ".join(e.get("selected_bullets", [])[:2])
        for e in selected.get("selected_experience", [])[:4]
    )
    proj_summary = "\n".join(
        f"- {p['title']} ({p['role']}): " + (p["bullets"][0]["text"] if p.get("bullets") else "")
        for p in components.get("projects", [])
    )
    edu = components.get("education", [{}])[0]

    prompt = f"""Write 3 professional cover letter body paragraphs for this job application.

JOB: {job_info.get('job_title')} at {job_info.get('company')}
Requirements: {job_info.get('requirements', '')[:600]}
Responsibilities: {job_info.get('responsibilities', '')[:600]}

CANDIDATE:
Education: {edu.get('degree')} from {edu.get('institution')}, graduated December 2024
Skills: {chr(10).join(skills_lines[:6])}
Work Experience:
{exp_summary}
Personal Projects:
{proj_summary}

Write exactly 3 paragraphs (no headings, no labels, no numbering):
1. (4-6 sentences) Connect CS education and foundational technical skills to this specific role. Reference relevant coursework, the degree, and how it prepared the candidate.
2. (4-6 sentences) Highlight specific technical projects and work experience that directly match this job's requirements. Name actual projects and technologies used.
3. (4-6 sentences) Express genuine enthusiasm for {job_info.get('company')} specifically. Mention the company's focus area. Close with eagerness to contribute, grow, and work with the team.

Tone: professional, confident, specific. Name the company and role. No filler phrases.
Return a JSON array of exactly 3 strings. Return only valid JSON, no markdown."""

    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.1},
    )
    raw = _strip_llm_raw(response["message"]["content"])

    try:
        paragraphs = json.loads(raw)
        if isinstance(paragraphs, list) and len(paragraphs) >= 3:
            return [str(p).strip() for p in paragraphs[:3]]
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback to component templates
    paras = components.get("cover_letter_paragraphs", {})
    keys  = [k for k in selected.get("selected_cover_paragraphs", []) if k != "closing"]
    return [paras[k]["text"].strip() for k in keys if k in paras]


def build_cover_letter_docx(job_info: dict, selected: dict,
                            components: dict, output_path: Path,
                            paragraphs: list[str]):
    doc = Document()

    for section in doc.sections:
        section.top_margin    = Inches(1.0)
        section.bottom_margin = Inches(1.0)
        section.left_margin   = Inches(1.0)
        section.right_margin  = Inches(1.0)

    p_info  = components["personal"]
    title   = job_info.get("job_title", "this position")
    company = job_info.get("company", "your organization")

    def _p(text: str, space_after: float = 12):
        para = doc.add_paragraph(text)
        para.paragraph_format.space_before = Pt(0)
        para.paragraph_format.space_after  = Pt(space_after)
        if para.runs:
            para.runs[0].font.size = Pt(11)
        return para

    _p(datetime.now().strftime("%B %d, %Y"))
    _p("Dear Hiring Manager:")
    _p(f"I am writing to express my strong interest in the {title} position at {company}.")

    for body_para in paragraphs:
        _p(body_para)

    _p(
        f"If you have any questions, you can reach me at {p_info['phone']} or by email at "
        f"{p_info['email']}. I look forward to an opportunity to discuss this position further."
    )
    _p("Sincerely,", space_after=4)
    _p(p_info["name"], space_after=0)

    doc.save(str(output_path))


def generate_documents(job_info: dict, selected: dict, components: dict) -> dict:
    print(f"\n{'='*60}")
    print("STEP 3 — Generating tailored documents")
    print(f"{'='*60}")

    company_clean = re.sub(r'[^\w\s-]', '', job_info.get("company", "Company"))
    company_clean = company_clean.strip().replace(" ", "_")
    date_str      = datetime.now().strftime("%Y-%m-%d")

    resume_path = OUTPUT_DIR / f"resume_{company_clean}_{date_str}.docx"
    cover_path  = OUTPUT_DIR / f"cover_letter_{company_clean}_{date_str}.docx"

    build_resume_docx(job_info, selected, components, resume_path)

    print("  Generating cover letter content...")
    cover_paragraphs = generate_cover_letter_text(job_info, selected, components)
    build_cover_letter_docx(job_info, selected, components, cover_path, cover_paragraphs)

    print(f"\n  Resume saved       : {resume_path}")
    print(f"  Cover letter saved : {cover_path}")

    if selected.get("flagged_fields"):
        print(f"\n  ⚠️  Missing from your library — left blank:")
        for f in selected["flagged_fields"]:
            print(f"      - {f}")

    return {
        "resume_path":       str(resume_path),
        "cover_letter_path": str(cover_path),
    }


# ── Step 4: Inspect form ──────────────────────────────────────────────────────

def inspect_application_form(url: str, headless: bool = False) -> list[dict]:
    print(f"\n{'='*60}")
    print("STEP 4 — Inspecting application form")
    print(f"{'='*60}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page    = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        fields = page.evaluate("""() => {
            const results = [];
            const inputs = document.querySelectorAll('input, textarea, select');
            inputs.forEach(el => {
                if (el.type === 'hidden' || el.type === 'submit') return;
                let label = '';
                if (el.id) {
                    const lbl = document.querySelector('label[for="' + el.id + '"]');
                    if (lbl) label = lbl.innerText.trim();
                }
                if (!label) label = el.placeholder || el.name || el.type || 'unknown';
                results.push({
                    label:      label,
                    field_type: el.tagName.toLowerCase() + (el.type ? '[' + el.type + ']' : ''),
                    required:   el.required,
                    name:       el.name || '',
                    id:         el.id || '',
                });
            });
            return results;
        }""")

        browser.close()

    print(f"\n  Found {len(fields)} form fields:")
    for f in fields[:20]:
        req = "REQUIRED" if f["required"] else "optional"
        print(f"    [{req}] {f['label']} ({f['field_type']})")

    return fields


# ── Step 5: Fill form ─────────────────────────────────────────────────────────

def fill_application_form(url: str, job_info: dict, docs: dict,
                          components: dict, fields: list,
                          headless: bool = False) -> dict:
    print(f"\n{'='*60}")
    print("STEP 5 — Filling application form")
    print("  Browser will open so you can watch.")
    print(f"{'='*60}")

    p_info = components["personal"]

    resume_text = ""
    try:
        resume_doc = Document(docs["resume_path"])
        resume_text = "\n".join([p.text for p in resume_doc.paragraphs])
    except Exception:
        pass

    prompt = f"""You are filling out a job application form.

Personal info:
- Name: {p_info['name']}
- Email: {p_info['email']}
- Phone: {p_info['phone']}
- Location: {p_info['location']}
- Portfolio: {p_info['portfolio']}
- LinkedIn: {p_info['linkedin']}

Resume summary:
{resume_text[:2000]}

Form fields:
{json.dumps([f for f in fields[:20] if f.get("id") or f.get("name")], indent=2)}

For each field decide what value to fill. Return ONLY a JSON array where each item has:
- "id": field id
- "name": field name
- "value": what to type (empty string if unknown)
- "flagged": true if information is not available

Return only valid JSON array, no markdown."""

    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    raw = _strip_llm_raw(response["message"]["content"])

    try:
        fill_plan = json.loads(raw)
    except json.JSONDecodeError:
        fill_plan = []

    flagged = [f for f in fill_plan if f.get("flagged") and (f.get("name") or f.get("id"))]
    if flagged:
        print(f"\n  ⚠️  {len(flagged)} field(s) flagged as unknown — will be left blank:")
        for f in flagged:
            print(f"      - {f.get('name') or f.get('id', 'unknown')}")

    filled_summary = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page    = pw.chromium.launch(headless=headless) if False else browser.new_page()
        page    = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        # ── Upload resume and cover letter if file fields exist ──────────────
        resume_path_str = docs.get("resume_path", "")
        cover_path_str  = docs.get("cover_letter_path", "")
        for file_id, file_path in [("resume", resume_path_str), ("cover_letter", cover_path_str)]:
            if not file_path or not os.path.exists(file_path):
                continue
            try:
                el = page.query_selector(f"#{file_id}")
                if el:
                    el.set_input_files(file_path)
                    filled_summary.append({"field": file_id, "value": f"[file: {Path(file_path).name}]"})
                    print(f"  Uploaded {file_id}: {Path(file_path).name}")
            except Exception as e:
                print(f"  Warning: Could not upload '{file_id}': {e}")

        # ── Fill standard text/tel fields ────────────────────────────────────
        for field_plan in fill_plan:
            value = field_plan.get("value", "")
            if not value or field_plan.get("flagged"):
                continue
            field_id   = field_plan.get("id", "")
            field_name = field_plan.get("name", "")
            try:
                selector = f"#{field_id}" if field_id else f"[name='{field_name}']"
                el = page.query_selector(selector)
                if el:
                    input_type = el.get_attribute("type") or ""
                    tag = el.evaluate("el => el.tagName.toLowerCase()")
                    if input_type == "file":
                        continue  # already handled above
                    if tag in ("input", "textarea"):
                        el.fill(value)
                        filled_summary.append({"field": field_id or field_name, "value": value[:50]})
            except Exception as e:
                print(f"  Warning: Could not fill '{field_id or field_name}': {e}")

        # ── Fill Greenhouse dropdown/select questions via visible label click ─
        greenhouse_answers = {
            "At the time of applying, are you 18 years of age or older": "Yes",
            "Are you authorized to work in the United States": "Yes",
            "Will you now, or in the future, require sponsorship": "No",
            "Have you ever worked for a Sony company previously": "No",
        }
        for question_fragment, answer in greenhouse_answers.items():
            try:
                # Find the question label, then find its associated select or radio
                matched = page.evaluate(f"""() => {{
                    const labels = [...document.querySelectorAll('label, legend, div, span')];
                    const lbl = labels.find(l => l.innerText && l.innerText.includes({json.dumps(question_fragment)}));
                    if (!lbl) return false;
                    // Try to find the nearest select
                    const parent = lbl.closest('div.field, fieldset, div') || lbl.parentElement;
                    if (!parent) return false;
                    const sel = parent.querySelector('select');
                    if (sel) {{
                        const opts = [...sel.options];
                        const opt = opts.find(o => o.text.trim().toLowerCase().startsWith({json.dumps(answer.lower())}));
                        if (opt) {{ sel.value = opt.value; sel.dispatchEvent(new Event('change', {{bubbles:true}})); return true; }}
                    }}
                    // Try radio buttons
                    const radios = [...(parent.querySelectorAll('input[type=radio]') || [])];
                    const radio = radios.find(r => {{
                        const rl = document.querySelector('label[for="'+r.id+'"]');
                        return rl && rl.innerText.trim().toLowerCase().startsWith({json.dumps(answer.lower())});
                    }});
                    if (radio) {{ radio.click(); return true; }}
                    return false;
                }}""")
                if matched:
                    filled_summary.append({"field": question_fragment[:40], "value": answer})
                    print(f"  Answered: '{question_fragment[:40]}...' → {answer}")
            except Exception as e:
                print(f"  Warning: Greenhouse answer failed for '{question_fragment[:40]}': {e}")

        # ── Fill "How did you hear" ───────────────────────────────────────────
        try:
            el = page.query_selector("#question_15583143004")
            if el:
                el.fill("LinkedIn")
                filled_summary.append({"field": "How did you hear", "value": "LinkedIn"})
        except Exception:
            pass

        print(f"\n  Filled {len(filled_summary)} field(s).")
        print("\n  ⏸  Form is filled but NOT submitted.")
        print("  Review the browser window, then return here.")
        input("  Press ENTER when ready to proceed to final approval...")
        browser.close()

    return {"filled_fields": filled_summary, "flagged": flagged}


# ── Step 6: Final approval ────────────────────────────────────────────────────

def present_summary(job_info: dict, docs: dict, fill_result: dict, selected: dict):
    print(f"\n{'='*60}")
    print("STEP 6 — APPLICATION SUMMARY FOR REVIEW")
    print(f"{'='*60}")
    print(f"  Job Title   : {job_info.get('job_title')}")
    print(f"  Company     : {job_info.get('company')}")
    print(f"  URL         : {job_info.get('url')}")
    print(f"\n  Resume      : {docs['resume_path']}")
    print(f"  Cover Letter: {docs['cover_letter_path']}")
    print(f"\n  Fields filled  : {len(fill_result.get('filled_fields', []))}")

    flagged = [
        f for f in fill_result.get("flagged", [])
        if f.get("name") or f.get("id")
    ] + [{"name": f} for f in selected.get("flagged_fields", [])]
    if flagged:
        print(f"\n  ⚠️  Items requiring your review:")
        for f in flagged:
            print(f"      - {f.get('name') or f.get('id', str(f))}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Job Application Agent")
    parser.add_argument("--url",      required=True, help="Job posting URL")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser headlessly (no visible window)")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("JOB APPLICATION AGENT")
    print("Pauses at each step for your approval.")
    print("="*60)

    components = load_components()

    # ── STEP 1 ────────────────────────────────────────────────────────────────
    job_info = read_job_description(args.url, headless=args.headless)
    log_action("read_job_description",
               job_info.get("job_title",""), job_info.get("company",""), "success")

    if not ask_approval(
        "Job description read",
        f"Title  : {job_info.get('job_title')}\n"
        f"Company: {job_info.get('company')}\n\n"
        f"Summary: {job_info.get('summary','')[:400]}"
    ):
        log_action("step_1_approval", job_info.get("job_title",""), job_info.get("company",""), "rejected")
        print("Stopped after Step 1.")
        return

    # ── STEP 2 ────────────────────────────────────────────────────────────────
    selected = select_components(job_info, components)
    log_action("selected_components",
               job_info.get("job_title",""), job_info.get("company",""), "success")

    if not ask_approval(
        "Component selection complete — review and approve",
        f"Skills     : {selected.get('selected_skills')}\n"
        f"Experience : {[e['company'] for e in selected.get('selected_experience',[])]}\n"
        f"Projects   : {selected.get('selected_projects')}\n"
        f"Cover paras: {selected.get('selected_cover_paragraphs')}\n"
        f"Flagged    : {selected.get('flagged_fields', [])}"
    ):
        log_action("step_2_approval", job_info.get("job_title",""), job_info.get("company",""), "rejected")
        print("Stopped after Step 2.")
        return

    # ── STEP 3 ────────────────────────────────────────────────────────────────
    docs = generate_documents(job_info, selected, components)
    log_action("generated_documents",
               job_info.get("job_title",""), job_info.get("company",""), "success",
               f"Resume: {docs['resume_path']}")

    if not ask_approval(
        "Documents generated — open and review them before continuing",
        f"Resume      : {docs['resume_path']}\n"
        f"Cover Letter: {docs['cover_letter_path']}\n\n"
        f"Open these files now to review, then return here."
    ):
        log_action("step_3_approval", job_info.get("job_title",""), job_info.get("company",""), "rejected")
        print("Stopped after Step 3. Documents saved.")
        return

    # ── STEP 4 ────────────────────────────────────────────────────────────────
    apply_url = job_info.get("apply_url", args.url)
    fields = inspect_application_form(apply_url, headless=args.headless)
    log_action("inspected_form",
               job_info.get("job_title",""), job_info.get("company",""), "success",
               f"{len(fields)} fields found")

    if not ask_approval(
        f"Form inspection complete — {len(fields)} fields found",
        "Proceed to fill the form?"
    ):
        log_action("step_4_approval", job_info.get("job_title",""), job_info.get("company",""), "rejected")
        print("Stopped after Step 4.")
        return

    # ── STEP 5 ────────────────────────────────────────────────────────────────
    fill_result = fill_application_form(
        apply_url, job_info, docs, components, fields, headless=False
    )
    log_action("filled_form",
               job_info.get("job_title",""), job_info.get("company",""), "success",
               f"{len(fill_result.get('filled_fields',[]))} filled, "
               f"{len(fill_result.get('flagged',[]))} flagged")

    # ── STEP 6 ────────────────────────────────────────────────────────────────
    present_summary(job_info, docs, fill_result, selected)

    if not ask_approval(
        "FINAL APPROVAL — Submit the application?",
        "⚠️  This will submit your application. This cannot be undone.\n"
        "Review all flagged items above before proceeding."
    ):
        log_action("final_submission",
                   job_info.get("job_title",""), job_info.get("company",""),
                   "rejected", "User chose not to submit")
        print("\nApplication NOT submitted. Documents saved.")
        print(f"Log: {ACTIONS_LOG}")
        return

    print("\n  NOTE: Automated submission not yet implemented.")
    print("  Please submit manually using the saved documents.")
    log_action("submission_attempt",
               job_info.get("job_title",""), job_info.get("company",""),
               "flagged", "Manual submission required")

    print(f"\n✅ Complete.")
    print(f"   Resume      : {docs['resume_path']}")
    print(f"   Cover Letter: {docs['cover_letter_path']}")
    print(f"   Log         : {ACTIONS_LOG}")


if __name__ == "__main__":
    main()
