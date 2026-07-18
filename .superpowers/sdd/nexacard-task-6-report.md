# Task 6 Report: On-Demand NexaCard Login Recovery

## Delivered

- Added `NexaCardLogin.ensure_authenticated(page, settings) -> bool` in
  `nexacard_otp/login.py`.
- Recovery is invoked only by callers of `ensure_authenticated`; it creates no
  background task or polling loop.
- Known authenticated NexaCard pages return `False` without accessing login
  controls or Gmail. Logout detection uses the `/login` URL plus the stable
  username-input placeholder as form evidence.
- A shared login lock serializes recovery. The waiting caller re-navigates and
  checks state inside the lock, so it skips duplicate login after another
  caller has restored the session.
- Native controls use the confirmed placeholder and DOM selectors for account,
  password, email verification choice, verification email, request-code,
  nine-digit code, and submit. The aware UTC timestamp is captured directly
  before requesting the verification code.
- Every navigation, fill, click, and final URL wait has a 30-second bound.
  Gmail timeout, auth/refresh, temporary failures, and Playwright failures are
  translated to causal, secret-free `NexaCardLoginFailed` errors.

## TDD and verification

- RED: `tests.test_nexacard_login` initially failed with
  `ModuleNotFoundError: No module named 'nexacard_otp.login'`.
- GREEN: focused suite: 11 tests passed.
- Compile check: `python -m compileall -q nexacard_otp tests` passed.
- Full suite: `python -m unittest discover -s tests -p 'test_*.py' -v` passed
  (320 tests).
- `git diff --check` passed.

## Self-review / residual test concern

- All Task 6 page and Gmail interactions are mocked; no test opens a real
  browser or makes a live request.
- The selectors are locked by tests. A future NexaCard markup change requires
  an intentional selector update plus test adjustment; this is preferable to
  fragile text-only matching.
- Task 7 payment-OTP lookup and polling were intentionally not implemented.
