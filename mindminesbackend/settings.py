
from pathlib import Path
from datetime import timedelta
import os, environ

env = environ.Env()



# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
STATICFILES_DIRS = [BASE_DIR / "static"]  # adjust if yours differs

environ.Env.read_env(os.path.join(BASE_DIR, ".env")) 
# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-7^uze)9(a$8&l#o&$h&6%x)t!es*(uvf3gqpcod$z%xrqi@)2y'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

ALLOWED_HOSTS = [
    "mindmines.eu",
    "www.mindmines.eu",
    "127.0.0.1",
    "localhost",
]

AUTH_USER_MODEL = 'server.User'

ACCOUNT_LOGIN_METHODS = { "email"}  # allow both
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]

# Optional:
ACCOUNT_EMAIL_VERIFICATION = "mandatory"  # or "mandatory" 
ACCOUNT_CONFIRM_EMAIL_ON_GET = True

DEFAULT_FROM_EMAIL = "info@mindmines.eu"
ORDERS_NOTIF_EMAIL = "info@mindmines.eu"  # admin inbox for returns/cancels
INFO_EMAIL = "info@mindmines.eu"
ORDERS_ADMIN_EMAIL = "info@mindmines.eu"
EMAIL_LOGO_URL = "https://mindmines.eu/djstatic/emails/logo.png"
EMAIL_LOGO_PATH = "static/emails/logo.png"

# Application definition
SITE_NAME = "MindMines"
FRONTEND_URL = "https://mindmines.eu"
SUPPORT_EMAIL = "servicet@mindmines.eu"
COMPANY_ADDRESS = "MindMines · Hamburg, Germany"
EMAIL_HEADER_TAGLINE = "Fine natural stone décor, crafted with care."
ORDERS_BCC_EMAILS = ["service@mindmines.eu"]

INSTALLED_APPS = [
    'jazzmin',
    
     'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    "django.contrib.sitemaps",
    'corsheaders',
    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'dj_rest_auth',
    'dj_rest_auth.registration',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'allauth.socialaccount.providers.facebook',
    'social_django',
    'djoser',
    'django_filters',
    'server.apps.ServerConfig',
    "csp",
]
SITE_ID = 1


REST_USE_JWT = True

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # must be high
    'social_django.middleware.SocialAuthExceptionMiddleware',
    'django.middleware.security.SecurityMiddleware',
    "csp.middleware.CSPMiddleware",
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware'
]

ROOT_URLCONF = 'mindminesbackend.urls'

CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = (
    "'self'",
    "https://js.stripe.com",
    "https://www.paypal.com",
    "https://www.paypalobjects.com",
)
CSP_STYLE_SRC = (
    "'self'",
    "'unsafe-inline'",
    "https://fonts.googleapis.com",
)
CSP_FONT_SRC = (
    "'self'",
    "https://fonts.gstatic.com",
)
CSP_IMG_SRC = ("'self'", "data:", "https:")

CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": ("'self'",),
        "font-src": ("'self'", "https://fonts.gstatic.com"),
        "img-src": ("'self'", "data:", "https:"),
        "script-src": (
            "'self'",
            "https://js.stripe.com",
            "https://www.paypal.com",
            "https://www.paypalobjects.com",
        ),
        "style-src": (
            "'self'",
            "'unsafe-inline'",
            "https://fonts.googleapis.com",
        ),
    }
}

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'social_django.context_processors.backends',
                'social_django.context_processors.login_redirect'
            ],
        },
    },
]

WSGI_APPLICATION = 'mindminesbackend.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE':  "django.db.backends.postgresql",
        "NAME": env("POST_DB_NAME"),
        "USER": env("POST_DB_USER"),
        "PASSWORD":env('POST_DB_PWD'),
        "HOST": "localhost",
        "PORT": "5432",
    }
}

# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = "/djstatic/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# If using user uploads:
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email
EMAIL_TIMEOUT=10
#EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
EMAIL_BACKEND ="django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST =  "smtp.titan.email"
EMAIL_PORT =  "587"
EMAIL_USE_TLS =  "1" in ("1","true","True")
EMAIL_USE_SSL = "0" in ("1","true","True")  # must NOT be true with TLS

EMAIL_HOST_USER =  "info@mindmines.eu"
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")

# IMPORTANT: send *from* the same address (or a verified alias)
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Optional: users who reply should go here
ADMIN_EMAIL =EMAIL_HOST_USER

ADMINS = [("Errors", "info@mindmines.eu")] 

# Where to point verification links
FRONTEND_URL =  "https://mindmines.eu"

FRONTEND_URL_API  =  "https://mindmines.eu/api"

# Templates DIR fix (use absolute path)
TEMPLATES[0]["DIRS"] = [BASE_DIR / "templates"]
ORDER_EMAIL_REPLY_TO = ["info@mindmines.eu"]

# Optional dev toggle: only use file backend if you explicitly turn it on
USE_FILE_EMAIL_IN_DEBUG =  "0" in ("1", "true", "True")
if DEBUG and USE_FILE_EMAIL_IN_DEBUG:
    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    EMAIL_FILE_PATH = BASE_DIR / "sent_emails"

# allauth (new-style config)
ACCOUNT_USER_MODEL_USERNAME_FIELD = None



# CORS / CSRF for Vite dev on 5173
CORS_ALLOWED_ORIGINS = [
     "https://mindmines.eu",
   "https://www.mindmines.eu",
]
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]
CORS_ALLOW_METHODS = ["GET", "POST", "OPTIONS"]
CORS_ALLOW_CREDENTIALS = True   # <-- correct name (not CORS_ALLOWS_*)
CSRF_TRUSTED_ORIGINS = [
    "https://mindmines.eu",
    "https://www.mindmines.eu",
]
SECURE_SSL_REDIRECT = True

SECURE_HSTS_SECONDS = 63072000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True

X_FRAME_OPTIONS = "SAMEORIGIN"



# If you deploy to a domain, add it here too.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True


REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticatedOrReadOnly"],
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework_simplejwt.authentication.JWTAuthentication"],
    "DEFAULT_PAGINATION_CLASS": "server.pagination.StandardResultsSetPagination", 
    "PAGE_SIZE": 10,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend", "rest_framework.filters.SearchFilter"],
    "version": 1,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "user": "100/min",
        "anon": "50/min",
        "guest_lookup": "10/min",   # used by GuestLookupThrottle + public invoice download
    },
}


LOGGING = {
    "version": 1,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "DEBUG"},
        "rest_framework": {"handlers": ["console"], "level": "DEBUG"},
    },
}


AUTHENTICATION_BACKENDS = [
    'social_core.backends.google.GoogleOAuth2',
    'social_core.backends.facebook.FacebookOAuth2',
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]




SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = env("GOOGLE_OAUTH_CLIENT_ID")
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET =env("GOOGLE_OAUTH_CLIENT_SECRET")
SOCIAL_AUTH_GOOGLE_OAUTH2_SCOPE = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]
SOCIAL_AUTH_GOOGLE_OAUTH2_EXTRA_DATA = ["first_name", "last_name"]



SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    "AUTH_HEADER_TYPES": ("Bearer",),    
}

PASSWORD_RESET_TIMEOUT = 60 * 60 * 3


# --- Payments env (use .env in real) ---
PAYPAL_CLIENT_ID = env("PAY_PAL_CLIENT_ID", default="")
PAYPAL_CLIENT_SECRET = env("PAY_PAL_CLIENT_SECRET", default="")
PAYPAL_BRAND_NAME = env("PAYPAL_BRAND_NAME", default="MindMines")
PAYPAL_WEBHOOK_ID = env("PAYPAL_WEBHOOK_ID", default="")
PAYPAL_ENV = env("PAYPAL_ENV", default="live")

PAYPAL_BASE = (
    "https://api-m.paypal.com"
)

STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY",    default="")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY",    default="")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET",   default="")
STRIPE_CURRENCY = "eur"

BANK_TRANSFER = {
    "IBAN": env("BANK_IBAN", default="DE35 2415 1116 0000 6827 73"),
    "BIC": env("BANK_BIC", default="NOLADE21STK"),
    "ACCOUNT_NAME": env("BANK_ACCOUNT_NAME", default="MindMines GbR"),
    "PAYMENT_DUE_HOURS": int(env("BANK_PAYMENT_DUE_HOURS", default=24)),  # auto-cancel window
}


