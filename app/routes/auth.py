"""Register, log in, log out, profile/settings (SPEC 4, 10).

Registration needs full name, email, PH mobile (stored as E.164
``+639XXXXXXXXX``), a password that passes the strength rule
(utils.validators.password_problems) and Privacy Notice consent. The mobile
number is verified later by SMS OTP (notifications.verify_phone) before the
user can receive SMS match alerts.
"""

from __future__ import annotations

import sqlalchemy as sa
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, StringField, TelField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError

from app.extensions import config_limit, db, limiter
from app.models import User
from app.models.enums import PhotoOwner
from app.models.user import dummy_password_check
from app.services import storage_service
from app.services.audit_service import log_event
from app.services.storage_service import ImageValidationError
from app.utils.http import safe_next_url
from app.utils.phone import normalize_ph
from app.utils.time import utcnow
from app.utils.validators import PASSWORD_MAX_LENGTH, password_problems

bp = Blueprint("auth", __name__)

PASSWORD_HINT = "At least 8 characters, with at least one letter and one number."


def _normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _email_taken(email: str, exclude_user_id: int | None = None) -> bool:
    stmt = sa.select(User.id).where(User.email == email)  # emails are stored lower-case
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return db.session.scalar(stmt.limit(1)) is not None


def _mobile_taken(e164: str, exclude_user_id: int | None = None) -> bool:
    stmt = sa.select(User.id).where(User.mobile_e164 == e164)
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    return db.session.scalar(stmt.limit(1)) is not None


class MobileField(TelField):
    """A PH mobile number field; ``field.e164`` holds the normalised value after validation."""

    e164: str | None = None


def validate_ph_mobile(form: FlaskForm, field: MobileField) -> None:
    try:
        field.e164 = normalize_ph(field.data)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None


# --- Forms -------------------------------------------------------------------------------------


class RegisterForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    email = EmailField(
        "Email address",
        validators=[DataRequired(), Email(check_deliverability=False), Length(max=255)],
    )
    mobile = MobileField("Mobile number", validators=[DataRequired(), validate_ph_mobile])
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)]
    )
    confirm_password = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords do not match.")],
    )
    consent = BooleanField(
        "I have read the Privacy Notice",
        validators=[DataRequired(message="Please accept the Privacy Notice to continue.")],
    )

    def validate_email(self, field: EmailField) -> None:
        if _email_taken(_normalize_email(field.data)):
            raise ValidationError("An account with this email already exists.")

    def validate_mobile(self, field: MobileField) -> None:
        if field.e164 and _mobile_taken(field.e164):
            raise ValidationError("This mobile number is already registered.")

    def validate_password(self, field: PasswordField) -> None:
        problems = password_problems(field.data, email=self.email.data, name=self.full_name.data)
        if problems:
            raise ValidationError(" ".join(problems))


class LoginForm(FlaskForm):
    email = EmailField("Email address", validators=[DataRequired(), Length(max=255)])
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)]
    )
    remember = BooleanField("Keep me logged in")


class ProfileForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    email = EmailField(
        "Email address",
        validators=[DataRequired(), Email(check_deliverability=False), Length(max=255)],
    )
    mobile = MobileField("Mobile number", validators=[DataRequired(), validate_ph_mobile])
    sms_opt_in = BooleanField("Send me SMS alerts for new possible matches")
    push_opt_in = BooleanField("Send me push notifications on this and my other devices")

    def __init__(self, user: User, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = user

    def validate_email(self, field: EmailField) -> None:
        if _email_taken(_normalize_email(field.data), exclude_user_id=self._user.id):
            raise ValidationError("Another account already uses this email.")

    def validate_mobile(self, field: MobileField) -> None:
        if field.e164 and _mobile_taken(field.e164, exclude_user_id=self._user.id):
            raise ValidationError("Another account already uses this mobile number.")


class PasswordChangeForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField(
        "New password", validators=[DataRequired(), Length(max=PASSWORD_MAX_LENGTH)]
    )
    confirm_new_password = PasswordField(
        "Confirm new password",
        validators=[DataRequired(), EqualTo("new_password", message="Passwords do not match.")],
    )


# --- Views -------------------------------------------------------------------------------------


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit(config_limit("RATE_LIMIT_REGISTER"), methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    form = RegisterForm()
    if form.validate_on_submit():
        now = utcnow()
        user = User(
            full_name=form.full_name.data.strip(),
            email=_normalize_email(form.email.data),
            mobile_e164=form.mobile.e164,
            privacy_consented_at=now,
            last_login_at=now,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.flush()
        log_event("auth.register", actor=user, target=user)
        db.session.commit()
        login_user(user)
        flash(
            "Welcome to PawBalik! Verify your mobile number to receive SMS alerts "
            "about possible matches.",
            "success",
        )
        return redirect(safe_next_url(url_for("notifications.verify_phone")))
    return render_template("register.html", form=form, password_hint=PASSWORD_HINT)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(config_limit("RATE_LIMIT_LOGIN"), methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(safe_next_url(url_for("main.index")))
    form = LoginForm()
    if form.validate_on_submit():
        email = _normalize_email(form.email.data)
        user = db.session.scalar(sa.select(User).where(User.email == email))
        if user is None:
            dummy_password_check(form.password.data)  # same timing as a real check
        if user is None or not user.check_password(form.password.data):
            log_event(
                "auth.login_failed",
                target=user,
                details={"email": email[:255], "reason": "bad_credentials"},
                commit=True,
            )
            flash("Incorrect email or password.", "error")
            return render_template("login.html", form=form), 401
        if not user.is_active:
            log_event("auth.login_failed", target=user, details={"reason": "inactive"}, commit=True)
            flash("This account has been deactivated. Please contact the administrator.", "error")
            return render_template("login.html", form=form), 403
        login_user(user, remember=form.remember.data)
        user.last_login_at = utcnow()
        log_event("auth.login", actor=user, target=user)
        db.session.commit()  # also saves an upgraded password hash, if any
        flash(f"Welcome back, {user.first_name}!", "success")
        return redirect(safe_next_url(url_for("main.index")))
    return render_template("login.html", form=form)


@bp.post("/logout")
def logout():
    if current_user.is_authenticated:
        log_event("auth.logout", actor=current_user, target=current_user._get_current_object())
        db.session.commit()
        logout_user()
        flash("You have been logged out.", "info")
    return redirect(url_for("main.index"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user: User = current_user._get_current_object()
    form = ProfileForm(user, obj=None if request.method == "POST" else user)
    if request.method == "GET":
        form.mobile.data = user.mobile_e164 or ""
    if form.validate_on_submit():
        changed_mobile = form.mobile.e164 != user.mobile_e164
        user.full_name = form.full_name.data.strip()
        user.email = _normalize_email(form.email.data)
        user.set_mobile(form.mobile.e164)  # a new number must be verified again
        user.sms_opt_in = bool(form.sms_opt_in.data)
        user.push_opt_in = bool(form.push_opt_in.data)
        log_event(
            "auth.profile_updated",
            actor=user,
            target=user,
            details={"mobile_changed": changed_mobile},
        )
        db.session.commit()
        if changed_mobile:
            flash("Profile saved. Please verify your new mobile number.", "success")
            return redirect(url_for("notifications.verify_phone"))
        flash("Profile saved.", "success")
        return redirect(url_for("auth.profile"))
    return render_template(
        "profile.html",
        form=form,
        password_form=PasswordChangeForm(prefix="pw"),
        password_hint=PASSWORD_HINT,
    )


@bp.post("/profile/password")
@login_required
def change_password():
    user: User = current_user._get_current_object()
    password_form = PasswordChangeForm(prefix="pw")
    if password_form.validate_on_submit():
        if not user.check_password(password_form.current_password.data):
            password_form.current_password.errors.append("Your current password is incorrect.")
        else:
            problems = password_problems(
                password_form.new_password.data, email=user.email, name=user.full_name
            )
            if problems:
                password_form.new_password.errors.append(" ".join(problems))
            else:
                user.set_password(password_form.new_password.data)
                log_event("auth.password_changed", actor=user, target=user)
                db.session.commit()
                flash("Your password has been changed.", "success")
                return redirect(url_for("auth.profile"))
    form = ProfileForm(user, obj=user, formdata=None)
    form.mobile.data = user.mobile_e164 or ""
    return (
        render_template(
            "profile.html", form=form, password_form=password_form, password_hint=PASSWORD_HINT
        ),
        400,
    )


# --- Profile picture ---------------------------------------------------------------------------


@bp.post("/profile/avatar")
@login_required
@limiter.limit(config_limit("RATE_LIMIT_AVATAR"))
def upload_avatar():
    """Upload or replace the profile picture (form field ``avatar``)."""
    user: User = current_user._get_current_object()
    upload = request.files.get("avatar")
    if upload is None or not upload.filename:
        flash("Choose a photo to use as your profile picture.", "error")
        return redirect(url_for("auth.profile"))
    old_photo = user.avatar_photo
    try:
        photo = storage_service.save_image(
            upload,
            owner_type=PhotoOwner.AVATAR,
            owner_id=user.id,
            folder=storage_service.default_folder(PhotoOwner.AVATAR, user.id),
            is_primary=True,
        )
    except ImageValidationError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("auth.profile"))
    user.avatar_photo = photo
    log_event("auth.avatar_updated", actor=user, target=user)
    db.session.commit()
    if old_photo is not None:
        _delete_avatar_photo(old_photo)
    flash("Profile picture updated.", "success")
    return redirect(url_for("auth.profile"))


@bp.post("/profile/avatar/delete")
@login_required
def delete_avatar():
    """Remove the profile picture; the initial is shown again."""
    user: User = current_user._get_current_object()
    old_photo = user.avatar_photo
    if old_photo is not None:
        user.avatar_photo = None
        log_event("auth.avatar_removed", actor=user, target=user)
        db.session.commit()
        _delete_avatar_photo(old_photo)
        flash("Profile picture removed.", "success")
    return redirect(url_for("auth.profile"))


def _delete_avatar_photo(photo) -> None:  # noqa: ANN001
    """Delete a replaced/removed avatar from storage and the database (best effort)."""
    try:
        storage_service.delete_photo(photo)
        db.session.commit()
    except Exception:  # noqa: BLE001 - a leftover file must not break the profile page
        db.session.rollback()
