# 2026-11-10 语音服务部署文档

## 原因
用户要求将 streaming-speech 加入 README、服务端口及启动说明，并配置服务 URL。

## 做了什么
补齐独立启动、8600 端口、CPU/GPU 安装入口、健康检查、词表维护、浏览器经 Gateway 转发及发布包边界。确认 speech_service_url 已在 constant_config.json、Gateway 默认值及两份 RuntimeConfig 类型中存在，保留默认 http://127.0.0.1:8600，不重复增加配置。用户在途 disable_agent_details 配置改动不纳入本提交。功能口径沿用 §7.6/§9.1，无新接口或功能变更。

## 验证
核对启动脚本参数、配置默认值与服务 /health 字段；JSON 解析、共享类型逐字一致和 git diff --check 通过。仅文档变化，不重复运行功能测试。

## 进度
| 项 | 值 |
|---|---|
| 状态 | 已完成 |
| 完成日期 | 2026-11-10 |
| 落地 | 本任务本地提交 |
