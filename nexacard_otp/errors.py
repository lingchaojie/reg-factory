class NexaCardOtpError(RuntimeError):
    code = "nexacard_otp_error"


class InvalidLookupInput(NexaCardOtpError):
    code = "invalid_lookup_input"


class GmailAuthorizationRequired(NexaCardOtpError):
    code = "gmail_authorization_required"


class GmailTemporarilyUnavailable(NexaCardOtpError):
    code = "gmail_temporarily_unavailable"


class NexaCardLoginFailed(NexaCardOtpError):
    code = "nexacard_login_failed"


class NexaCardPageError(NexaCardOtpError):
    code = "nexacard_page_error"


class NexaCardTransientError(NexaCardOtpError):
    code = "nexacard_temporarily_unavailable"


class OtpLookupTimedOut(NexaCardOtpError):
    code = "otp_lookup_timed_out"
