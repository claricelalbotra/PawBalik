-- =============================================================================
-- PawBalik database schema (MySQL 8 / MariaDB 10.4+, InnoDB, utf8mb4)
--
-- GENERATED FILE - do not edit by hand.
-- Source: the SQLAlchemy models in app/models/.
-- Regenerate after changing a model:   python run.py export-schema
-- Create the tables:                   python run.py init-db
--   (or import this file in phpMyAdmin: Import > choose schema.sql > Go)
--
-- Every statement uses IF NOT EXISTS, so importing it twice is harmless.
-- It does NOT alter existing tables: after a model change on a database that
-- already has data, drop the affected tables (or the whole database) first.
-- =============================================================================


CREATE DATABASE IF NOT EXISTS pawbalik
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE pawbalik;

-- Table: assistance_providers

CREATE TABLE IF NOT EXISTS assistance_providers (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(160) NOT NULL,
	type ENUM('rescuer','vet','shelter') NOT NULL,
	address VARCHAR(255),
	barangay VARCHAR(120),
	lat DOUBLE,
	lng DOUBLE,
	phones VARCHAR(255),
	facebook_url VARCHAR(255),
	hours_text VARCHAR(255),
	is_24_7 BOOL NOT NULL,
	services TEXT,
	is_active BOOL NOT NULL,
	is_sample BOOL NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	CONSTRAINT pk_assistance_providers PRIMARY KEY (id),
	INDEX ix_assistance_providers_is_active (is_active),
	INDEX ix_assistance_providers_lat_lng (lat, lng),
	INDEX ix_assistance_providers_type (type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: case_counters

CREATE TABLE IF NOT EXISTS case_counters (
	year INTEGER NOT NULL,
	`last_value` INTEGER NOT NULL,
	CONSTRAINT pk_case_counters PRIMARY KEY (year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: jobs

CREATE TABLE IF NOT EXISTS jobs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	type VARCHAR(64) NOT NULL,
	payload JSON NOT NULL,
	status ENUM('pending','running','done','failed') NOT NULL,
	attempts INTEGER NOT NULL,
	last_error TEXT,
	run_after DATETIME NOT NULL,
	locked_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	CONSTRAINT pk_jobs PRIMARY KEY (id),
	INDEX ix_jobs_status_run_after (status, run_after),
	INDEX ix_jobs_type (type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: photos

CREATE TABLE IF NOT EXISTS photos (
	id INTEGER NOT NULL AUTO_INCREMENT,
	owner_type ENUM('lost_report','sighting','scan','message','avatar') NOT NULL,
	owner_id INTEGER,
	storage ENUM('cloudinary','local') NOT NULL,
	public_id VARCHAR(255) NOT NULL,
	secure_url VARCHAR(512),
	width INTEGER,
	height INTEGER,
	sha256 VARCHAR(64) NOT NULL,
	is_primary BOOL NOT NULL,
	sort_order INTEGER NOT NULL,
	ai_attributes JSON,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_photos PRIMARY KEY (id),
	CONSTRAINT uq_photos_public_id UNIQUE (public_id),
	INDEX ix_photos_owner (owner_type, owner_id),
	INDEX ix_photos_sha256 (sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: image_comparisons

CREATE TABLE IF NOT EXISTS image_comparisons (
	id INTEGER NOT NULL AUTO_INCREMENT,
	query_sha256 VARCHAR(64) NOT NULL,
	candidate_photo_id INTEGER NOT NULL,
	model_name VARCHAR(100) NOT NULL,
	result JSON NOT NULL,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_image_comparisons PRIMARY KEY (id),
	CONSTRAINT uq_image_comparisons_query_sha256_candidate_photo_id_model_name UNIQUE (query_sha256, candidate_photo_id, model_name),
	CONSTRAINT fk_image_comparisons_candidate_photo_id_photos FOREIGN KEY(candidate_photo_id) REFERENCES photos (id) ON DELETE CASCADE,
	INDEX ix_image_comparisons_candidate_photo_id (candidate_photo_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: users

CREATE TABLE IF NOT EXISTS users (
	id INTEGER NOT NULL AUTO_INCREMENT,
	full_name VARCHAR(120) NOT NULL,
	email VARCHAR(255) NOT NULL,
	mobile_e164 VARCHAR(16),
	mobile_verified_at DATETIME,
	password_hash VARCHAR(255) NOT NULL,
	`role` ENUM('user','admin') NOT NULL,
	is_active BOOL NOT NULL,
	sms_opt_in BOOL NOT NULL,
	push_opt_in BOOL NOT NULL,
	last_login_at DATETIME,
	privacy_consented_at DATETIME,
	avatar_photo_id INTEGER,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	CONSTRAINT pk_users PRIMARY KEY (id),
	CONSTRAINT uq_users_email UNIQUE (email),
	CONSTRAINT uq_users_mobile_e164 UNIQUE (mobile_e164),
	CONSTRAINT fk_users_avatar_photo_id_photos FOREIGN KEY(avatar_photo_id) REFERENCES photos (id) ON DELETE SET NULL,
	INDEX ix_users_avatar_photo_id (avatar_photo_id),
	INDEX ix_users_role (`role`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: audit_logs

CREATE TABLE IF NOT EXISTS audit_logs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	actor_id INTEGER,
	event VARCHAR(64) NOT NULL,
	target_type VARCHAR(40),
	target_id INTEGER,
	ip VARCHAR(45),
	details JSON,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_audit_logs PRIMARY KEY (id),
	CONSTRAINT fk_audit_logs_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id),
	INDEX ix_audit_logs_actor_id (actor_id),
	INDEX ix_audit_logs_created_at (created_at),
	INDEX ix_audit_logs_event (event),
	INDEX ix_audit_logs_target (target_type, target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: device_tokens

CREATE TABLE IF NOT EXISTS device_tokens (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER NOT NULL,
	fcm_token VARCHAR(512) NOT NULL,
	user_agent VARCHAR(255),
	last_seen_at DATETIME,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_device_tokens PRIMARY KEY (id),
	CONSTRAINT fk_device_tokens_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	CONSTRAINT uq_device_tokens_fcm_token UNIQUE (fcm_token),
	INDEX ix_device_tokens_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: lost_reports

CREATE TABLE IF NOT EXISTS lost_reports (
	id INTEGER NOT NULL AUTO_INCREMENT,
	case_number VARCHAR(20) NOT NULL,
	owner_id INTEGER NOT NULL,
	pet_name VARCHAR(80) NOT NULL,
	species ENUM('dog','cat') NOT NULL,
	breed VARCHAR(80) NOT NULL,
	colors VARCHAR(120) NOT NULL,
	size ENUM('small','medium','large') NOT NULL,
	sex ENUM('male','female','unknown') NOT NULL,
	age_text VARCHAR(40),
	identifying_features TEXT,
	last_seen_lat DOUBLE NOT NULL,
	last_seen_lng DOUBLE NOT NULL,
	last_seen_place VARCHAR(160),
	last_seen_at DATETIME NOT NULL,
	contact_name VARCHAR(120) NOT NULL,
	contact_email VARCHAR(255),
	contact_phone VARCHAR(16),
	show_phone BOOL NOT NULL,
	reward_amount NUMERIC(10, 2),
	reward_text VARCHAR(160),
	status ENUM('lost','found','closed') NOT NULL,
	is_hidden BOOL NOT NULL,
	hidden_reason VARCHAR(255),
	hidden_by INTEGER,
	hidden_at DATETIME,
	ai_attributes JSON,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	CONSTRAINT pk_lost_reports PRIMARY KEY (id),
	CONSTRAINT uq_lost_reports_case_number UNIQUE (case_number),
	CONSTRAINT fk_lost_reports_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id),
	CONSTRAINT fk_lost_reports_hidden_by_users FOREIGN KEY(hidden_by) REFERENCES users (id),
	INDEX ix_lost_reports_hidden_by (hidden_by),
	INDEX ix_lost_reports_last_seen_lat_lng (last_seen_lat, last_seen_lng),
	INDEX ix_lost_reports_owner_id (owner_id),
	INDEX ix_lost_reports_pool (status, is_hidden, species),
	INDEX ix_lost_reports_species (species),
	INDEX ix_lost_reports_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: notifications

CREATE TABLE IF NOT EXISTS notifications (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER NOT NULL,
	type VARCHAR(40) NOT NULL,
	title VARCHAR(160) NOT NULL,
	body VARCHAR(500),
	link_url VARCHAR(512),
	is_read BOOL NOT NULL,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_notifications PRIMARY KEY (id),
	CONSTRAINT fk_notifications_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	INDEX ix_notifications_user_id (user_id),
	INDEX ix_notifications_user_id_is_read (user_id, is_read)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: phone_verifications

CREATE TABLE IF NOT EXISTS phone_verifications (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER NOT NULL,
	mobile_e164 VARCHAR(16) NOT NULL,
	code_hash VARCHAR(255) NOT NULL,
	expires_at DATETIME NOT NULL,
	attempts INTEGER NOT NULL,
	consumed_at DATETIME,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_phone_verifications PRIMARY KEY (id),
	CONSTRAINT fk_phone_verifications_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	INDEX ix_phone_verifications_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: sightings

CREATE TABLE IF NOT EXISTS sightings (
	id INTEGER NOT NULL AUTO_INCREMENT,
	finder_id INTEGER NOT NULL,
	species ENUM('dog','cat') NOT NULL,
	breed VARCHAR(80),
	colors VARCHAR(120),
	size ENUM('small','medium','large'),
	`condition` ENUM('healthy','injured','unknown') NOT NULL,
	is_holding_animal BOOL NOT NULL,
	notes TEXT,
	seen_lat DOUBLE NOT NULL,
	seen_lng DOUBLE NOT NULL,
	seen_place VARCHAR(160),
	seen_at DATETIME NOT NULL,
	contact_name VARCHAR(120) NOT NULL,
	contact_email VARCHAR(255),
	contact_phone VARCHAR(16),
	contact_pref ENUM('in_app','phone') NOT NULL,
	status ENUM('active','matched','expired') NOT NULL,
	is_hidden BOOL NOT NULL,
	hidden_reason VARCHAR(255),
	hidden_by INTEGER,
	hidden_at DATETIME,
	ai_attributes JSON,
	expires_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	CONSTRAINT pk_sightings PRIMARY KEY (id),
	CONSTRAINT fk_sightings_finder_id_users FOREIGN KEY(finder_id) REFERENCES users (id),
	CONSTRAINT fk_sightings_hidden_by_users FOREIGN KEY(hidden_by) REFERENCES users (id),
	INDEX ix_sightings_expires_at (expires_at),
	INDEX ix_sightings_finder_id (finder_id),
	INDEX ix_sightings_hidden_by (hidden_by),
	INDEX ix_sightings_pool (status, is_hidden, species),
	INDEX ix_sightings_seen_lat_lng (seen_lat, seen_lng),
	INDEX ix_sightings_species (species),
	INDEX ix_sightings_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: sms_log

CREATE TABLE IF NOT EXISTS sms_log (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER,
	to_number VARCHAR(16) NOT NULL,
	message VARCHAR(640) NOT NULL,
	purpose VARCHAR(30) NOT NULL,
	provider VARCHAR(30) NOT NULL,
	provider_message_id VARCHAR(100),
	status VARCHAR(20) NOT NULL,
	error TEXT,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_sms_log PRIMARY KEY (id),
	CONSTRAINT fk_sms_log_user_id_users FOREIGN KEY(user_id) REFERENCES users (id),
	INDEX ix_sms_log_created_at (created_at),
	INDEX ix_sms_log_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: case_status_history

CREATE TABLE IF NOT EXISTS case_status_history (
	id INTEGER NOT NULL AUTO_INCREMENT,
	lost_report_id INTEGER NOT NULL,
	from_status ENUM('lost','found','closed'),
	to_status ENUM('lost','found','closed') NOT NULL,
	changed_by INTEGER,
	note VARCHAR(255),
	changed_at DATETIME NOT NULL,
	CONSTRAINT pk_case_status_history PRIMARY KEY (id),
	CONSTRAINT fk_case_status_history_lost_report_id_lost_reports FOREIGN KEY(lost_report_id) REFERENCES lost_reports (id) ON DELETE CASCADE,
	CONSTRAINT fk_case_status_history_changed_by_users FOREIGN KEY(changed_by) REFERENCES users (id),
	INDEX ix_case_status_history_changed_by (changed_by),
	INDEX ix_case_status_history_lost_report_id (lost_report_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: conversations

CREATE TABLE IF NOT EXISTS conversations (
	id INTEGER NOT NULL AUTO_INCREMENT,
	lost_report_id INTEGER NOT NULL,
	sighting_id INTEGER,
	owner_id INTEGER NOT NULL,
	other_user_id INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	last_message_at DATETIME,
	CONSTRAINT pk_conversations PRIMARY KEY (id),
	CONSTRAINT uq_conversations_lost_report_id_other_user_id UNIQUE (lost_report_id, other_user_id),
	CONSTRAINT fk_conversations_lost_report_id_lost_reports FOREIGN KEY(lost_report_id) REFERENCES lost_reports (id),
	CONSTRAINT fk_conversations_sighting_id_sightings FOREIGN KEY(sighting_id) REFERENCES sightings (id),
	CONSTRAINT fk_conversations_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id),
	CONSTRAINT fk_conversations_other_user_id_users FOREIGN KEY(other_user_id) REFERENCES users (id),
	INDEX ix_conversations_last_message_at (last_message_at),
	INDEX ix_conversations_lost_report_id (lost_report_id),
	INDEX ix_conversations_other_user_id (other_user_id),
	INDEX ix_conversations_owner_id (owner_id),
	INDEX ix_conversations_sighting_id (sighting_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: push_log

CREATE TABLE IF NOT EXISTS push_log (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER,
	token_id INTEGER,
	title VARCHAR(160) NOT NULL,
	status VARCHAR(20) NOT NULL,
	error TEXT,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_push_log PRIMARY KEY (id),
	CONSTRAINT fk_push_log_user_id_users FOREIGN KEY(user_id) REFERENCES users (id),
	CONSTRAINT fk_push_log_token_id_device_tokens FOREIGN KEY(token_id) REFERENCES device_tokens (id) ON DELETE SET NULL,
	INDEX ix_push_log_created_at (created_at),
	INDEX ix_push_log_token_id (token_id),
	INDEX ix_push_log_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: rescue_requests

CREATE TABLE IF NOT EXISTS rescue_requests (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER NOT NULL,
	lost_report_id INTEGER,
	sighting_id INTEGER,
	provider_id INTEGER,
	summary_text TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_rescue_requests PRIMARY KEY (id),
	CONSTRAINT fk_rescue_requests_user_id_users FOREIGN KEY(user_id) REFERENCES users (id),
	CONSTRAINT fk_rescue_requests_lost_report_id_lost_reports FOREIGN KEY(lost_report_id) REFERENCES lost_reports (id),
	CONSTRAINT fk_rescue_requests_sighting_id_sightings FOREIGN KEY(sighting_id) REFERENCES sightings (id),
	CONSTRAINT fk_rescue_requests_provider_id_assistance_providers FOREIGN KEY(provider_id) REFERENCES assistance_providers (id),
	INDEX ix_rescue_requests_lost_report_id (lost_report_id),
	INDEX ix_rescue_requests_provider_id (provider_id),
	INDEX ix_rescue_requests_sighting_id (sighting_id),
	INDEX ix_rescue_requests_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: scans

CREATE TABLE IF NOT EXISTS scans (
	id INTEGER NOT NULL AUTO_INCREMENT,
	user_id INTEGER NOT NULL,
	mode ENUM('found','lost') NOT NULL,
	`trigger` ENUM('manual','background') NOT NULL,
	query_photo_id INTEGER,
	source_lost_report_id INTEGER,
	source_sighting_id INTEGER,
	scan_lat DOUBLE,
	scan_lng DOUBLE,
	precheck JSON,
	attributes JSON,
	status ENUM('pending','running','complete','rejected','failed') NOT NULL,
	error TEXT,
	result_count INTEGER NOT NULL,
	gemini_ok BOOL,
	completed_at DATETIME,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_scans PRIMARY KEY (id),
	CONSTRAINT fk_scans_user_id_users FOREIGN KEY(user_id) REFERENCES users (id),
	CONSTRAINT fk_scans_query_photo_id_photos FOREIGN KEY(query_photo_id) REFERENCES photos (id),
	CONSTRAINT fk_scans_source_lost_report_id_lost_reports FOREIGN KEY(source_lost_report_id) REFERENCES lost_reports (id),
	CONSTRAINT fk_scans_source_sighting_id_sightings FOREIGN KEY(source_sighting_id) REFERENCES sightings (id),
	INDEX ix_scans_created_at (created_at),
	INDEX ix_scans_query_photo_id (query_photo_id),
	INDEX ix_scans_source_lost_report_id (source_lost_report_id),
	INDEX ix_scans_source_sighting_id (source_sighting_id),
	INDEX ix_scans_status (status),
	INDEX ix_scans_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: abuse_reports

CREATE TABLE IF NOT EXISTS abuse_reports (
	id INTEGER NOT NULL AUTO_INCREMENT,
	reporter_id INTEGER NOT NULL,
	target_user_id INTEGER NOT NULL,
	conversation_id INTEGER,
	reason TEXT NOT NULL,
	status ENUM('open','reviewed','dismissed') NOT NULL,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_abuse_reports PRIMARY KEY (id),
	CONSTRAINT fk_abuse_reports_reporter_id_users FOREIGN KEY(reporter_id) REFERENCES users (id),
	CONSTRAINT fk_abuse_reports_target_user_id_users FOREIGN KEY(target_user_id) REFERENCES users (id),
	CONSTRAINT fk_abuse_reports_conversation_id_conversations FOREIGN KEY(conversation_id) REFERENCES conversations (id),
	INDEX ix_abuse_reports_conversation_id (conversation_id),
	INDEX ix_abuse_reports_reporter_id (reporter_id),
	INDEX ix_abuse_reports_status (status),
	INDEX ix_abuse_reports_target_user_id (target_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: messages

CREATE TABLE IF NOT EXISTS messages (
	id INTEGER NOT NULL AUTO_INCREMENT,
	conversation_id INTEGER NOT NULL,
	sender_id INTEGER,
	type ENUM('text','location_pin','image','system') NOT NULL,
	body TEXT,
	lat DOUBLE,
	lng DOUBLE,
	location_label VARCHAR(160),
	photo_id INTEGER,
	read_at DATETIME,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_messages PRIMARY KEY (id),
	CONSTRAINT fk_messages_conversation_id_conversations FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE CASCADE,
	CONSTRAINT fk_messages_sender_id_users FOREIGN KEY(sender_id) REFERENCES users (id),
	CONSTRAINT fk_messages_photo_id_photos FOREIGN KEY(photo_id) REFERENCES photos (id),
	INDEX ix_messages_conversation_id (conversation_id),
	INDEX ix_messages_conversation_id_id (conversation_id, id),
	INDEX ix_messages_photo_id (photo_id),
	INDEX ix_messages_sender_id (sender_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: scan_results

CREATE TABLE IF NOT EXISTS scan_results (
	id INTEGER NOT NULL AUTO_INCREMENT,
	scan_id INTEGER NOT NULL,
	candidate_type ENUM('lost_report','sighting') NOT NULL,
	candidate_id INTEGER NOT NULL,
	candidate_photo_id INTEGER,
	visual_score FLOAT,
	attribute_score FLOAT NOT NULL,
	distance_km FLOAT,
	distance_score FLOAT NOT NULL,
	recency_score FLOAT NOT NULL,
	final_score FLOAT NOT NULL,
	tier ENUM('high','moderate','low','hidden') NOT NULL,
	`rank` INTEGER NOT NULL,
	visual_check_unavailable BOOL NOT NULL,
	explanation JSON,
	notified_at DATETIME,
	created_at DATETIME NOT NULL,
	CONSTRAINT pk_scan_results PRIMARY KEY (id),
	CONSTRAINT uq_scan_results_scan_id_candidate_type_candidate_id UNIQUE (scan_id, candidate_type, candidate_id),
	CONSTRAINT fk_scan_results_scan_id_scans FOREIGN KEY(scan_id) REFERENCES scans (id) ON DELETE CASCADE,
	CONSTRAINT fk_scan_results_candidate_photo_id_photos FOREIGN KEY(candidate_photo_id) REFERENCES photos (id),
	INDEX ix_scan_results_candidate (candidate_type, candidate_id),
	INDEX ix_scan_results_candidate_photo_id (candidate_photo_id),
	INDEX ix_scan_results_scan_id (scan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Table: sighting_access_log

CREATE TABLE IF NOT EXISTS sighting_access_log (
	id INTEGER NOT NULL AUTO_INCREMENT,
	sighting_id INTEGER NOT NULL,
	viewer_id INTEGER NOT NULL,
	scan_id INTEGER,
	accessed_at DATETIME NOT NULL,
	CONSTRAINT pk_sighting_access_log PRIMARY KEY (id),
	CONSTRAINT fk_sighting_access_log_sighting_id_sightings FOREIGN KEY(sighting_id) REFERENCES sightings (id) ON DELETE CASCADE,
	CONSTRAINT fk_sighting_access_log_viewer_id_users FOREIGN KEY(viewer_id) REFERENCES users (id),
	CONSTRAINT fk_sighting_access_log_scan_id_scans FOREIGN KEY(scan_id) REFERENCES scans (id),
	INDEX ix_sighting_access_log_scan_id (scan_id),
	INDEX ix_sighting_access_log_sighting_id (sighting_id),
	INDEX ix_sighting_access_log_viewer_id (viewer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
