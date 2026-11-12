-- MariaDB 10.6+ schema for the order site and the provisioning worker.
-- Create the database first (see README), then:  mariadb flask_orders < schema.sql
-- Money is stored in cents (integers) plus a 3-letter currency. Times are UTC.

CREATE TABLE IF NOT EXISTS plans (
  code                VARCHAR(40)  NOT NULL,
  product_type        ENUM('vm','domain') NOT NULL,
  title               VARCHAR(80)  NOT NULL,
  price_cents         INT UNSIGNED NOT NULL,
  currency            CHAR(3)      NOT NULL DEFAULT 'USD',
  billing_period      ENUM('monthly','yearly') NOT NULL,
  trial_days          SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  braintree_plan_id   VARCHAR(64)  NOT NULL,
  flavor              VARCHAR(40)  NULL,
  tld                 VARCHAR(10)  NULL,
  active              TINYINT(1)   NOT NULL DEFAULT 1,
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS customers (
  id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  email                  VARCHAR(254) NOT NULL,
  name                   VARCHAR(100) NOT NULL,
  braintree_customer_id  VARCHAR(36)  NOT NULL,
  created_at             DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_customers_email (email),
  UNIQUE KEY uq_customers_braintree (braintree_customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS payment_methods (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  customer_id      BIGINT UNSIGNED NOT NULL,
  braintree_token  VARCHAR(64) NOT NULL,
  card_type        VARCHAR(30) NULL,
  last4            CHAR(4)     NULL,
  expiration       VARCHAR(7)  NULL,
  created_at       DATETIME    NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_payment_methods_token (braintree_token),
  KEY ix_payment_methods_customer (customer_id),
  CONSTRAINT fk_payment_methods_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS orders (
  id                        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  public_id                 CHAR(36)    NOT NULL,
  customer_id               BIGINT UNSIGNED NOT NULL,
  plan_code                 VARCHAR(40) NOT NULL,
  product_type              ENUM('vm','domain') NOT NULL,
  status                    ENUM('pending','active','past_due','cancelled','failed') NOT NULL DEFAULT 'pending',
  fulfillment_status        ENUM('pending','in_progress','done','failed','needs_review') NOT NULL DEFAULT 'pending',
  fulfillment_attempts      TINYINT UNSIGNED NOT NULL DEFAULT 0,
  fulfillment_error         VARCHAR(255) NULL,
  amount_cents              INT UNSIGNED NOT NULL,
  currency                  CHAR(3)     NOT NULL DEFAULT 'USD',
  billing_period            ENUM('monthly','yearly') NOT NULL,
  trial_days                SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  payment_method_id         BIGINT UNSIGNED NULL,
  braintree_subscription_id VARCHAR(64) NULL,
  next_billing_date         DATE        NULL,
  failure_reason            VARCHAR(255) NULL,
  created_at                DATETIME    NOT NULL,
  updated_at                DATETIME    NOT NULL,
  cancelled_at              DATETIME    NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_orders_public (public_id),
  UNIQUE KEY uq_orders_subscription (braintree_subscription_id),
  KEY ix_orders_customer (customer_id),
  KEY ix_orders_fulfillment (status, fulfillment_status),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id),
  CONSTRAINT fk_orders_plan FOREIGN KEY (plan_code) REFERENCES plans (code),
  CONSTRAINT fk_orders_payment_method FOREIGN KEY (payment_method_id) REFERENCES payment_methods (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS vm_orders (
  order_id             BIGINT UNSIGNED NOT NULL,
  vm_name              VARCHAR(63) NOT NULL,
  flavor               VARCHAR(40) NOT NULL,
  openstack_server_id  VARCHAR(64) NULL,
  floating_ip          VARCHAR(45) NULL,
  PRIMARY KEY (order_id),
  UNIQUE KEY uq_vm_orders_name (vm_name),
  CONSTRAINT fk_vm_orders_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS domain_orders (
  order_id                  BIGINT UNSIGNED NOT NULL,
  domain_name               VARCHAR(253) NOT NULL,
  tld                       VARCHAR(10)  NOT NULL,
  price_cents               INT UNSIGNED NOT NULL,
  currency                  CHAR(3)      NOT NULL DEFAULT 'USD',
  consent_at                DATETIME     NOT NULL,
  registrar                 ENUM('fake','godaddy') NULL,
  registrar_registration_id VARCHAR(64)  NULL,
  registrar_cost_cents      INT UNSIGNED NULL,
  registered_at             DATETIME     NULL,
  expires_at                DATE         NULL,
  years_paid                TINYINT UNSIGNED NOT NULL DEFAULT 0,
  auto_renew                TINYINT(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (order_id),
  UNIQUE KEY uq_domain_orders_name (domain_name),
  CONSTRAINT fk_domain_orders_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS transactions (
  id                        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id                  BIGINT UNSIGNED NOT NULL,
  customer_id               BIGINT UNSIGNED NOT NULL,
  braintree_transaction_id  VARCHAR(64) NOT NULL,
  kind                      ENUM('sale','refund') NOT NULL,
  amount_cents              INT UNSIGNED NOT NULL,
  currency                  CHAR(3)     NOT NULL DEFAULT 'USD',
  status                    VARCHAR(40) NOT NULL,
  processor_response        VARCHAR(120) NULL,
  occurred_at               DATETIME    NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_transactions_braintree (braintree_transaction_id),
  KEY ix_transactions_order (order_id),
  KEY ix_transactions_customer (customer_id),
  CONSTRAINT fk_transactions_order FOREIGN KEY (order_id) REFERENCES orders (id),
  CONSTRAINT fk_transactions_customer FOREIGN KEY (customer_id) REFERENCES customers (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS braintree_events (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  kind             VARCHAR(60) NOT NULL,
  subscription_id  VARCHAR(64) NULL,
  payload_hash     CHAR(64)    NOT NULL,
  received_at      DATETIME    NOT NULL,
  error            VARCHAR(255) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_braintree_events_hash (payload_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS godaddy_requests (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  order_id       BIGINT UNSIGNED NULL,
  action         ENUM('availability','register','renew','auto_renew_off') NOT NULL,
  mode           ENUM('fake','live') NOT NULL,
  domain_name    VARCHAR(253) NOT NULL,
  status         ENUM('ok','error') NOT NULL,
  cost_cents     INT UNSIGNED NULL,
  registration_id VARCHAR(64) NULL,
  detail         VARCHAR(500) NULL,
  created_at     DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY ix_godaddy_requests_order (order_id),
  KEY ix_godaddy_requests_domain (domain_name),
  CONSTRAINT fk_godaddy_requests_order FOREIGN KEY (order_id) REFERENCES orders (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Stand-in for the domain registry while REGISTRAR=fake. Not used with real GoDaddy.
CREATE TABLE IF NOT EXISTS fake_registry (
  domain_name      VARCHAR(253) NOT NULL,
  registration_id  VARCHAR(64)  NOT NULL,
  cost_cents       INT UNSIGNED NOT NULL,
  registered_at    DATETIME     NOT NULL,
  expires_at       DATE         NOT NULL,
  auto_renew       TINYINT(1)   NOT NULL DEFAULT 0,
  PRIMARY KEY (domain_name),
  UNIQUE KEY uq_fake_registry_reg (registration_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
