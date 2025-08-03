import re


# Разбивка markdown на секции по "## Title"
def split_markdown_sections(md_text):
    pattern = re.compile(r"^##\s+(.+)", re.MULTILINE)
    matches = list(pattern.finditer(md_text))
    sections = {}
    for idx, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
        content = md_text[start:end].strip()
        sections[title] = content
    return sections


# Чистка и нормализация отдельной секции
def clean_section_md(text, section):
    import re

    errors = []
    text = re.sub(
        r"(\bIn summary\b|\bOverall\b|\bВ целом\b|\bПодытожим\b)[\s\S]*$",
        "",
        text,
        flags=re.I,
    )

    if section["title"].lower() == "features" and section["type"] == "list":
        cleaned_lines = []
        lines = text.splitlines()
        for idx, raw_line in enumerate(lines):
            line = raw_line.strip()
            line = re.sub(r"^[-*•\d\.\s]+", "", line)
            if not line:
                continue

            parts = []
            remains = line
            while True:
                bold_match = re.match(r"\*\*([^\*]+)\*\*", remains)
                if bold_match:
                    parts.append(bold_match.group(1).strip())
                    remains = remains[bold_match.end() :]
                    remains = re.sub(r"^[:\-\s]+", "", remains)
                else:
                    break
            if parts:
                tail_match = re.match(r"([^:]+):\s*(.+)", remains)
                if tail_match:
                    parts.append(tail_match.group(1).strip())
                    desc = tail_match.group(2).strip()
                else:
                    desc_match = re.match(r":\s*(.+)", remains)
                    if desc_match:
                        desc = desc_match.group(1).strip()
                    else:
                        desc = ""
                # ВАЖНО: Удаляем лишние звездочки из всех частей
                title = "-".join(parts).replace(" -", "-").replace("- ", "-")
                title = re.sub(r"--+", "-", title)
                title = re.sub(r"\*+", "", title)
                desc = re.sub(r"\*+", "", desc)
                if not desc:
                    errors.append(
                        f"Feature missing description on line {idx+1}: {raw_line}"
                    )
                cleaned_lines.append(f"- **{title}**: {desc}")
                continue

            # Классика: **Title**: Description
            m = re.match(r"\*{2,}([^\*]+?)\*{2,}[:\-\s]+(.+)", line)
            if m:
                title, desc = m.group(1).strip(), m.group(2).strip()
                title = re.sub(r"\*+", "", title)
                desc = re.sub(r"\*+", "", desc)
                if not desc:
                    errors.append(
                        f"Feature missing description on line {idx+1}: {raw_line}"
                    )
                cleaned_lines.append(f"- **{title}**: {desc}")
                continue

            # Без bold, до первого двоеточия/тире
            m2 = re.match(r"(.+?)[\:\-]\s+(.+)", line)
            if m2:
                title = m2.group(1).strip()
                desc = m2.group(2).strip()
                title = re.sub(r"\*+", "", title)
                desc = re.sub(r"\*+", "", desc)
                if not desc:
                    errors.append(
                        f"Feature missing description on line {idx+1}: {raw_line}"
                    )
                cleaned_lines.append(f"- **{title}**: {desc}")
                continue

            # Orphan bold (без описания)
            m3 = re.match(r"\*\*(.+?)\*\*", line)
            if m3:
                errors.append(
                    f"Feature missing description on line {idx+1}: {raw_line}"
                )
                continue

            # fallback - error
            errors.append(f"Unrecognized feature line {idx+1}: {raw_line}")

        if errors or not cleaned_lines:
            return "", errors
        return "\n".join(cleaned_lines), []

    return text.strip(), []


# Основная функция нормализации markdown по шаблону (без ретраев)
def normalize_content_to_template_md(raw_md, template, connection_title=None):
    sections = template["sections"]
    blocks = split_markdown_sections(raw_md)
    out_md = ""
    for section in sections:
        sec_title = section["title"]
        if sec_title == "{connection_title}" and connection_title:
            sec_title = connection_title
        found = None
        for k in blocks:
            if k.lower() == sec_title.lower():
                found = blocks[k]
                break
        # Удаляем пустой connection-раздел
        if section["title"] == "{connection_title}" and (
            not found or not found.strip()
        ):
            continue
        if found:
            cleaned, _ = clean_section_md(found, section)
            out_md += f"## {sec_title}\n\n{cleaned.strip()}\n\n"
        else:
            out_md += (
                f"## {sec_title}\n\n"
                + ("- " if section["type"] == "list" else "")
                + "\n\n"
            )
    return out_md.strip()


# Нормализация с поддержкой ретраев через AI (до 3 попыток)
def normalize_content_to_template_md_with_retry(
    raw_md, template, connection_title=None, ai_retry_func=None, max_retries=3
):
    for attempt in range(max_retries):
        sections = template["sections"]
        blocks = split_markdown_sections(raw_md)
        out_md = ""
        retry_needed = False

        for section in sections:
            sec_title = section["title"]
            if sec_title == "{connection_title}" and connection_title:
                sec_title = connection_title
            found = None
            for k in blocks:
                if k.lower() == sec_title.lower():
                    found = blocks[k]
                    break
            if section["title"] == "{connection_title}" and (
                not found or not found.strip()
            ):
                continue
            if found:
                cleaned, errors = clean_section_md(found, section)
                if errors or not cleaned.strip():
                    retry_needed = True
                    break
                out_md += f"## {sec_title}\n\n{cleaned.strip()}\n\n"
            else:
                out_md += (
                    f"## {sec_title}\n\n"
                    + ("- " if section["type"] == "list" else "")
                    + "\n\n"
                )
        if retry_needed:
            if ai_retry_func is not None:
                raw_md = ai_retry_func()
                continue
            else:
                return ""
        return out_md.strip()
    return out_md.strip()
