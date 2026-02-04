# MindMines Backend — Software Design Document (SDD)

## 1. Purpose and Scope
This document describes the architecture, core components, data model, and key flows for the MindMines backend. The backend is a Django + DRF service that powers user accounts, product catalog, orders/payments, marketing tracking, and transactional email operations for the MindMines storefront.

## 2. System Overview
- **Frameworks & runtime**: Django 5.x with Django REST Framework for API delivery, JWT authentication, and Allauth/social auth integrations. The backend is packaged as a Django project with a single primary app named `server`.【F:mindminesbackend/settings.py†L1-L112】
- **Data storage**: PostgreSQL is configured as the primary datastore, with environment-based credentials read from `.env`.【F:mindminesbackend/settings.py†L113-L153】
- **External services**:
  - **Stripe** for card/SEPA/redirect-based payments and webhooks.【F:server/views.py†L86-L220】【F:server/views.py†L1200-L1402】
  - **PayPal** for order creation and capture flow.【F:server/views.py†L1099-L1192】
  - **SMTP** email delivery via Titan email configuration.【F:mindminesbackend/settings.py†L183-L218】
- **Front-end integration**: API endpoints are mounted under `/api/`, while JWT endpoints are exposed at `/api/token/` and `/api/token/refresh/` for the frontend app at `https://mindmines.eu`.【F:mindminesbackend/urls.py†L1-L19】

## 3. High-Level Architecture
- **Project layout**:
  - `mindminesbackend/`: Django settings, WSGI/ASGI entrypoints, and root URL routing.【F:mindminesbackend/settings.py†L1-L218】【F:mindminesbackend/urls.py†L1-L19】【F:mindminesbackend/wsgi.py†L1-L16】【F:mindminesbackend/asgi.py†L1-L16】
  - `server/`: core domain app with models, serializers, views, admin customizations, and utilities for emails, invoices, and background helpers.【F:server/models.py†L1-L138】【F:server/serializers.py†L1-L119】【F:server/views.py†L1-L200】【F:server/admin.py†L1-L120】
  - `templates/`: HTML/TXT templates for registration, invoices, order confirmations, newsletters, and return/cancellation flows.【F:templates/registration/password_reset_email.html†L1-L20】【F:templates/emails/order_confirmation.html†L1-L40】【F:templates/emails/invoice.html†L1-L30】
- **Service boundaries**:
  - The backend is a single deployable service with an HTTP API. It integrates external payment providers and SMTP, and emits PDF invoices through server-side generation.

## 4. Core Domain Model
### 4.1 Users & Profiles
- **User**: Custom user model uses email as the login identifier, with an optional username field.【F:server/models.py†L88-L123】
- **Profile**: One-to-one with User; stores full name, mobile, avatar, verification status, Stripe customer ID, birthday, and preferred language for email localization.【F:server/models.py†L126-L160】
- **Login OTP**: Stores time-limited OTP hash for two-step login verification with metadata and rate limiting support.【F:server/models.py†L162-L212】

### 4.2 Addresses
- **Address**: Holds checkout-friendly fields (name, address lines, city, phone) plus legacy compatibility fields. Enforces a single default address per user and supports atomic default switching.【F:server/models.py†L218-L296】

### 4.3 Catalog & Content
- **NaturalStone** and **NaturalStoneImage**: Marketing content for stone types with localized descriptions and limited gallery images per stone.【F:server/models.py†L302-L343】
- **Product**: Rich bilingual catalog item with pricing logic (net/gross VAT), sale pricing, stock tracking, slug/key generation, and image resizing on save.【F:server/models.py†L350-L478】
- **ProductMedia**: Supports image/video gallery with per-product limits and auto-resize for images.【F:server/models.py†L482-L539】
- **ProductEvent**: Tracks analytics for views, wishlist adds, and cart adds used by admin demand reporting.【F:server/models.py†L542-L562】

### 4.4 Cart & Orders
- **Cart**: UUID-based cart for users and guests, with tokens for guest tracking and active flag for lifecycle management.【F:server/models.py†L611-L640】
- **CartItem**: Denormalized item snapshot with price/metadata for admin visibility.【F:server/models.py†L643-L663】
- **Order**: Core checkout entity storing shipping snapshot, pricing totals, payment references, status, invoice metadata, and stock reservation flags. Includes methods for total computation, invoice numbering, and stock lock/release helpers.【F:server/models.py†L671-L911】【F:server/models.py†L960-L1134】
- **OrderItem**: Snapshot of product name and unit price at purchase time with line total helper.【F:server/models.py†L1137-L1163】

### 4.5 Discounts & Promotions
- **Coupon**: Supports percent/fixed discounts, targeting (public, assigned users, allowed emails), category/product scoping, and usage limits.【F:server/models.py†L1180-L1316】
- **CouponRedemption**: Tracks coupon usage per order/user for limit enforcement.【F:server/models.py†L1319-L1329】
- **PromotionBanner**: Time-bounded marketing banner with styling, optional coupon linkage, and country targeting.【F:server/models.py†L1335-L1372】

### 4.6 Returns, Reviews, Invoices
- **ReturnRequest / ReturnAttachment**: Tracks customer cancellation/return requests with attachments.【F:server/models.py†L1378-L1396】
- **Review**: Product review with rating, optional image, and author metadata.【F:server/models.py†L1410-L1422】
- **Invoice**: One-to-one with Order, stores PDF files and invoice number.【F:server/models.py†L1399-L1408】

### 4.7 Marketing & Automation
- **MarketingEvent**: Generic tracking events for analytics/marketing with UTM data, optional product/order links, and JSON metadata.【F:server/models.py†L1443-L1532】
- **AbandonedCartReminder**: Logs reminders sent for carts to prevent repeated spam.【F:server/models.py†L1536-L1568】
- **BirthdayCouponLog**: Tracks birthday coupon sends per user/date to avoid duplicates.【F:server/models.py†L1426-L1440】

## 5. API Surface (High-Level)
All API routes are registered under `/api/` and grouped by domain in `server/urls.py`.

- **Auth & Account**: register, login OTP start/verify, profile, account updates, password reset, and email verification flows.【F:server/urls.py†L34-L77】【F:server/views.py†L236-L420】
- **Catalog & Content**: product listing/search/suggest/detail, natural stone content, and stone enquiry submission.【F:server/urls.py†L78-L110】【F:server/views.py†L460-L720】
- **Cart**: cart sync/merge for user/guest, admin cart list for support visibility.【F:server/urls.py†L102-L110】【F:server/views.py†L732-L864】
- **Orders**: create order, list orders, order detail (auth + public), invoice generation/download, and admin order updates/tracking.【F:server/urls.py†L120-L160】【F:server/views.py†L930-L1030】
- **Coupons & Promotions**: coupon preview and active promo retrieval.【F:server/urls.py†L167-L174】【F:server/views.py†L1038-L1088】
- **Payments**:
  - PayPal order creation/capture endpoints.【F:server/urls.py†L187-L189】【F:server/views.py†L1099-L1192】
  - Stripe PaymentIntent/setup/SEPA/redirect flows + webhook for payment status updates.【F:server/urls.py†L195-L210】【F:server/views.py†L1200-L1402】
- **Returns & Cancellations**: request return/cancel for an order.【F:server/urls.py†L217-L219】
- **Marketing Tracking**: event tracking endpoint and newsletter subscription logging.【F:server/urls.py†L182-L184】【F:server/views.py†L876-L914】
- **Stock Alerts & On-Demand Requests**: notify back-in-stock and capture on-demand requests.【F:server/urls.py†L224-L229】

## 6. Authentication & Authorization
- **JWT** is used for API auth (`MyTokenObtainPairSerializer` adds profile info).【F:server/serializers.py†L214-L230】
- **Email verification** and **password reset** flows are implemented with tokenized links to the frontend and HTML templates for email content.【F:server/views.py†L470-L626】【F:templates/registration/password_reset_email.html†L1-L20】
- **Login OTP** is supported for two-step email-based login flow with rate limiting and OTP hashes stored in `LoginOTP` model.【F:server/models.py†L162-L212】【F:server/views.py†L273-L360】

## 7. Payments & Order Lifecycle
- **Order creation** is handled by `OrderCreateView`, which validates payloads via `OrderCreateSerializer` and sends admin notification emails.【F:server/views.py†L944-L957】
- **Stripe** flows support card, SEPA, and redirect-based intents and can create or reuse orders from signed checkout tokens. Webhook handler updates payment status and triggers post-payment pipeline logic.【F:server/views.py†L1200-L1402】
- **PayPal** order creation and capture flows create a local `Order`, call PayPal APIs, and mark the order paid on completion.【F:server/views.py†L1099-L1192】
- **Stock reservation** is handled via `Order.reserve_stock_atomic()` and `Order.release_stock_atomic()` to ensure inventory integrity.【F:server/models.py†L960-L1034】

## 8. Emails, Templates, and Invoices
- **Email utility** builds bilingual transactional messages and attaches invoice PDFs when available, with logo URL derived from settings.【F:server/utils/emails.py†L1-L140】
- **Invoice generation** creates bilingual PDF invoices using ReportLab and optionally overlays them on a PDF template (`static/pdf/Mindmines template.pdf`).【F:server/utils/invoices.py†L1-L172】【F:server/utils/invoices.py†L175-L258】
- **Templates** include HTML/TXT versions for order confirmations, invoices, cancellations, returns, SEPA status, newsletter onboarding, and password reset flows.【F:templates/emails/order_confirmation.html†L1-L40】【F:templates/emails/invoice.html†L1-L30】【F:templates/emails/return_user.html†L1-L40】

## 9. Background Processing
- **Lightweight async utilities** (`fire_and_forget`) allow running non-critical tasks in a background thread (e.g., post-payment pipeline).【F:server/tasks.py†L1-L52】

## 10. Signals & Automation
- **Signal hooks** handle profile creation, order status email notifications, invoice generation, and stock alert emails on restock events. These are connected in `ServerConfig.ready()`.【F:server/apps.py†L1-L20】【F:server/signals.py†L1-L219】

## 11. Admin & Operations
- **Admin actions** provide bulk order updates (paid/shipped/delivered/cancelled) with optional invoice generation and email notifications for fulfillment workflows.【F:server/admin.py†L90-L180】

## 12. Security & Configuration Notes
- **CSP** and middleware configuration are defined in settings for secure browser policies and auth/cors handling.【F:mindminesbackend/settings.py†L51-L112】
- **Static/Media** configuration ensures separation of user uploads and static assets under `/djstatic/` and `/media/` paths.【F:mindminesbackend/settings.py†L165-L178】

## 13. Extensibility Considerations
- **Payments**: Additional providers can be added by mirroring Stripe/PayPal service logic in views and updating order payment fields.【F:server/views.py†L1200-L1402】
- **Marketing & analytics**: `MarketingEvent` is designed as a generic schema that can store additional attributes in JSON without migrations.【F:server/models.py†L1443-L1532】
- **Background jobs**: `post_payment_pipeline` can be replaced with Celery/RQ for scalable async processing without changing view contracts.【F:server/tasks.py†L33-L52】
