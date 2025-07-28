import re


# Нормализация markdown-контента по шаблону
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
        if found:
            cleaned = clean_section_md(found, section)
            out_md += f"## {sec_title}\n\n{cleaned.strip()}\n\n"
        else:
            out_md += (
                f"## {sec_title}\n\n"
                + ("- " if section["type"] == "list" else "")
                + "\n\n"
            )
    return out_md.strip()


# Разбивка markdown на секции вида "## Title" -> {title: content}
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


# Чистка конкретной секции
def clean_section_md(text, section):
    text = re.sub(
        r"(\bIn summary\b|\bOverall\b|\bВ целом\b|\bПодытожим\b)[\s\S]*$",
        "",
        text,
        flags=re.I,
    )
    # Для Features: оставить только списки (строки с - или *), убрать прочее
    if section["title"].lower() == "features" and section["type"] == "list":
        lines = [
            l for l in text.splitlines() if re.match(r"^(\*|-|\d+\.)\s", l.strip())
        ]
        return "\n".join(lines) if lines else "- "
    return text.strip()
