# server/utils/i18n_templates.py
from django.template.loader import render_to_string, TemplateDoesNotExist

def render_locale(basename: str, ctx: dict, lang: str, ext: str):
    """
    Try basename.{lang}.{ext} first, then basename.{ext}.
    Example: basename="emails/sepa_pending", lang="de", ext="html"
    """
    if lang:
        # normalize
        lang = (lang or "").lower().replace("_", "-")
        if lang.startswith("de"):
            lang_try = "de"
        else:
            lang_try = lang.split("-")[0]

        try:
            return render_to_string(f"{basename}.{lang_try}.{ext}", ctx)
        except TemplateDoesNotExist:
            pass
    return render_to_string(f"{basename}.{ext}", ctx)
