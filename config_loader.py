"""YAML configuration loader for the SLMP-to-MQTT gateway.

Defines a hierarchy of dataclasses that mirror the YAML structure, plus
load(), validate(), pretty(), and to_dict() helpers.

Config tree:
    Config
    -- input:   InputConfig   (slmp: SlmpConfig, modbus_tcp: ModbusTcpConfig)
    -- output:  OutputConfig  (mqtt: MqttConfig, ros2: Ros2Config)
    -- gateway:  GatewayConfig
    -- logging: LoggingConfig
"""

from dataclasses import asdict, dataclass, field

import yaml

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass
class SlmpConfig:
    """SLMP TCP connection parameters."""
    ip: str = "192.168.200.99"
    port: int = 30000
    number_of_bytes: int = 4


@dataclass
class ModbusTcpConfig:
    """Modbus TCP connection parameters."""
    ip: str = "192.168.200.99"
    port: int = 502
    unit_id: int = 1


@dataclass
class InputConfig:
    """Top-level input source selection and its sub-configs."""
    type: str = "slmp"
    slmp: SlmpConfig = field(default_factory=SlmpConfig)
    modbus_tcp: ModbusTcpConfig = field(default_factory=ModbusTcpConfig)


@dataclass
class MqttConfig:
    """MQTT broker connection and authentication parameters."""
    ip: str = "localhost"
    port: int = 1883
    username: str = "gateway_user"
    password: str = "gateway"
    prefix: str = "gateway"
    keepalive_s: int = 60


@dataclass
class Ros2Config:
    """ROS 2 node and topic configuration."""
    node_name: str = "plc_gateway"
    topic_prefix: str = "/gateway"


@dataclass
class OutputConfig:
    """Top-level output sink selection and its sub-configs."""
    type: str = "mqtt"
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    ros2: Ros2Config = field(default_factory=Ros2Config)


@dataclass
class GatewayConfig:
    """Operational parameters for the SLMP-to-output gateway."""
    scan_list: str = "scan_list.csv"
    status_refresh_ms: int = 100
    stats_refresh_ms: int = 60000
    scan_jitter_ms: int = 100
    stats_window: int = 1000
    register_stats_window: int = 10
    buffer_size: int = 1000000
    max_tries: int = 3
    socket_timeout_s: float = 1.0


@dataclass
class LoggingConfig:
    """Logging verbosity configuration."""
    level: str = "DEBUG"


@dataclass
class Config:
    """Root configuration tree for the SLMP-to-MQTT gateway."""
    input: InputConfig = field(default_factory=InputConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _merge(dataclass_instance, data: dict):
    """Recursively fill a dataclass from a dict, ignoring unknown keys."""
    for key, value in data.items():
        if not hasattr(dataclass_instance, key):
            continue
        current = getattr(dataclass_instance, key)
        if hasattr(current, '__dataclass_fields__') and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(dataclass_instance, key, value)


def to_dict(cfg: Config) -> dict:
    """Convert a Config instance to a nested plain dict."""
    return asdict(cfg)


def pretty(cfg: Config) -> str:
    """Return a YAML-formatted string showing only the active input and output sub-config.

    Args:
        cfg: Loaded and validated Config instance.

    Returns:
        Human-readable YAML string with inactive input/output variants omitted.
    """
    d = asdict(cfg)
    d["input"] = {"type": cfg.input.type, cfg.input.type: d["input"][cfg.input.type]}
    d["output"] = {"type": cfg.output.type, cfg.output.type: d["output"][cfg.output.type]}
    d["output"][cfg.output.type]["username"] = "***"
    d["output"][cfg.output.type]["password"] = "***"
    return yaml.dump(d, default_flow_style=False, sort_keys=False)


def validate(cfg: Config) -> list[str]:
    """Validate config values. Returns a list of error messages (empty if valid)."""
    errors = []

    if cfg.input.slmp.number_of_bytes not in (3, 4):
        errors.append(f"input.slmp.number_of_bytes must be 3 or 4, got {cfg.input.slmp.number_of_bytes}")

    if cfg.logging.level.upper() not in _VALID_LOG_LEVELS:
        errors.append(f"logging.level must be one of {sorted(_VALID_LOG_LEVELS)}, got {cfg.logging.level!r}")

    if cfg.gateway.stats_window < 1:
        errors.append(f"gateway.stats_window must be at least 1, got {cfg.gateway.stats_window}")

    if cfg.gateway.register_stats_window < 1:
        errors.append(f"gateway.register_stats_window must be at least 1, got {cfg.gateway.register_stats_window}")

    if cfg.gateway.max_tries < 1:
        errors.append(f"gateway.max_tries must be at least 1, got {cfg.gateway.max_tries}")

    if cfg.gateway.socket_timeout_s <= 0:
        errors.append(f"gateway.socket_timeout_s must be positive, got {cfg.gateway.socket_timeout_s}")

    return errors


def load(path: str) -> Config:
    """Load and validate a YAML config file, returning a Config instance.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Fully populated and validated Config instance.

    Raises:
        ValueError: If any configuration value fails validation.
        FileNotFoundError: If the file does not exist.
    """
    cfg = Config()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    _merge(cfg, data)
    errors = validate(cfg)
    if errors:
        raise ValueError("Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors))
    return cfg
