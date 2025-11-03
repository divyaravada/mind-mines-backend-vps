# server/utils/i18n_templates.py
from django.template.loader import render_to_string, TemplateDoesNotExist

def render_bilingual(template_en: str, template_de: str, ctx: dict) -> tuple[str, str]:
    """
    Render EN/DE templates if available; otherwise return empty string for that locale.
    """
    html_en = ""
    html_de = ""
    try:
        html_en = render_to_string(template_en, ctx)
    except TemplateDoesNotExist:
        pass
    try:
        html_de = render_to_string(template_de, ctx)
    except TemplateDoesNotExist:
        pass
    return html_en, html_de
