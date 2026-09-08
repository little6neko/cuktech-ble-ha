# CUKTECH Charger — Home Assistant 蓝牙直连

通过 Home Assistant 管理的本机蓝牙适配器或支持主动连接的 ESPHome Bluetooth Proxy，直接连接酷态科 10 号超级电能充 Ultra（`njcuk.fitting.ad1204`）。

本版本以 `gateway/main` 的直连实现为基础，移植 main 后续的协议与稳定性修复。无需 MQTT、独立 BLE Server 或专用 ESP32 网关固件。

## 功能

- C1 / C2 / C3 / USB-A 电压、电流、功率与总功率。
- 设备硬件协议号驱动的 PD / PPS / UFCS / QC 等协议显示。
- 端口开关、协议开关、场景模式、息屏时间、语言和倒计时。
- 连接控制、连接状态、自动重连和静默连接探测。
- 设备设置推送同步；长连接解密计数器回绕处理。

## 安装

需要 Home Assistant 2024.1 或更新版本，以及能够主动连接设备的蓝牙适配器或代理。

### HACS

1. 在 HACS 的自定义存储库中添加 `https://github.com/little6neko/cuktech-ble-ha`，类别选择 **Integration**。
2. 安装 CUKTECH Charger，重启 Home Assistant。
3. 在“设置 → 设备与服务 → 添加集成”中搜索 **CUKTECH Charger**。

### 手动安装

将本仓库的 `custom_components/cuktech_charger` 文件夹复制到 HA 配置目录下的 `custom_components/`，重启后添加集成。

## 配置

填写充电器的蓝牙 MAC 和 Token：

| 字段 | 内容 |
|---|---|
| 名称 | 设备名称，可选 |
| MAC | 例如 `AA:BB:CC:DD:EE:FF` |
| Token | 12 字节，即 24 位十六进制字符串 |
| BLE Key | 可选，16 字节，即 32 位十六进制字符串；当前登录协议使用 Token |

可以使用 [Xiaomi Cloud Tokens Extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) 获取设备信息。配置流程检查格式，连接与认证在后台进行；保存配置不代表认证已成功。

确保 HA 的 Bluetooth 集成已启用。使用 ESPHome Bluetooth Proxy 时，代理必须启用主动连接并有空闲连接槽。关闭米家等应用与充电器的现有蓝牙连接。

## 从旧版本更新

- 已使用 gateway 蓝牙直连版本：保留现有配置，替换集成文件并重启。已有配置项与实体 unique ID 保持不变。
- 从旧 MQTT 版本切换：停止旧网关，删除原 CUKTECH 配置项，然后用 MAC 和 Token 重新添加。旧配置不包含直连凭据，无法自动转换。
- 原 `ha_integration/custom_components/` 已整理为仓库根目录 `custom_components/`，符合 HACS 安装布局。

## 使用说明

- “连接控制”只管理此设备的蓝牙会话；断开后仍可通过该开关重新连接。
- 端口没有负载与端口被关闭是不同状态；开关显示使用设备的 PIID 16。
- 息屏时间：设备值 `1=5分钟、2=10分钟、3=30分钟、4=常亮、5=1分钟`。
- 协议显示优先使用 PIID 17/18；仅在未收到硬件协议信息时使用粗略电压推断。
- 写入保留 gateway 的传输 ACK 成功后更新状态的行为；后续设备推送会纠正缓存。设备没有提供应用层结果时，传输 ACK 并不能证明设置已生效。
- 认证失败会延长重试间隔。恢复仅针对此设备，不重置共享适配器、不重启 HA。

此版本不提供旧独立服务的 Web 面板、SQLite 充电会话记录、充电完成事件、巴法云或专用 ESP32 网关固件。功率历史可使用 HA Recorder 查看。

## 开发与测试

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
python -m pytest
ruff check custom_components tests tests_ha
```

真实 HA 加载测试位于 `tests_ha/`。在独立 Python 3.13 环境中安装 Home Assistant 2025.1.4 及其 Bluetooth/USB 依赖后，单独运行 `python -m pytest tests_ha`，避免单元测试的 HA 模拟模块影响结果。本仓库不配置 GitHub Actions 自动检查。

移植来源与验证范围见 [直连移植说明](docs/direct-bluetooth.md)。

## 致谢与许可

基于 [kairui1108 的原项目](https://github.com/kairui1108/cuktech-ble-ha-integration) 与 [1812z 的 HA 蓝牙直连实现](https://github.com/1812z/cuktech-ble-ha)。感谢 [cuktech-ble-controller](https://github.com/zhyzhaogit/cuktech-ble-controller)、[ha-cuk-ble](https://github.com/zuyan9/ha-cuk-ble) 与 Bleak 项目。

[MIT License](LICENSE)
