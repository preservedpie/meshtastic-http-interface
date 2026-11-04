CREATE DATABASE IF NOT EXISTS meshtastic_monitor;
USE meshtastic_monitor;

CREATE TABLE IF NOT EXISTS packets (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    received_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    topic         VARCHAR(255),
    packet_type   VARCHAR(50),
    from_node     VARCHAR(20),
    sender        VARCHAR(20),
    channel       VARCHAR(50),
    rssi          INT,
    snr           FLOAT,
    hop_start     INT,
    hops_away     INT,
    payload_json  TEXT,
    INDEX (received_at),
    INDEX (from_node),
    INDEX (packet_type)
);

CREATE TABLE IF NOT EXISTS nodes (
    node_id       VARCHAR(20) PRIMARY KEY,
    long_name     VARCHAR(100),
    short_name    VARCHAR(20),
    hw_model      VARCHAR(50),
    role          VARCHAR(50),
    last_seen     DATETIME,
    last_rssi     INT,
    last_snr      FLOAT,
    last_hops     INT
);

CREATE TABLE IF NOT EXISTS text_messages (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    received_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    from_node     VARCHAR(20),
    channel       VARCHAR(50),
    message       TEXT,
    rssi          INT,
    snr           FLOAT,
    hops_away     INT,
    packet_id     INT,
    FOREIGN KEY (packet_id) REFERENCES packets(id)
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(100),
    enabled       BOOLEAN DEFAULT TRUE,
    packet_type   VARCHAR(50),
    from_node     VARCHAR(20),
    channel       VARCHAR(50),
    keyword       VARCHAR(100),
    priority      INT DEFAULT 0,
    sound         VARCHAR(50) DEFAULT 'pushover',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Example seed rules
INSERT IGNORE INTO alert_rules (id, name, enabled, packet_type, channel, priority, sound)
    VALUES (1, 'Any text on Sub channel', 1, 'text', 'Sub', 0, 'pushover');

INSERT IGNORE INTO alert_rules (id, name, enabled, packet_type, keyword, priority, sound)
    VALUES (2, 'Emergency keyword', 1, 'text', NULL, 1, 'siren');
