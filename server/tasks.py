from .utils.invoices import generate_invoice_pdf
from .utils.emails import send_invoice_email, send_order_confirmation

def post_payment_pipeline(order_id: int):
    from .models import Order
    order = Order.objects.get(pk=order_id)
    inv = generate_invoice_pdf(order)   # create/attach PDF
    send_invoice_email(order)           # email invoice
    try:
        locale = getattr(getattr(order.user, "profile", None), "language", "en") or "en"
        send_order_confirmation(order, user_locale=locale)
    except Exception:
        pass
