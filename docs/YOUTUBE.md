# YouTube Music 分支

基于 javycoder/fnos_music_ext 2.10.0。保留原 MIT 许可证与上游接管模型。
源码维护分支：`codex/youtube-music`，FPK 显示版本为 `2.10.0-2`。

## 使用

在飞牛桌面打开「fnMusic 音源扩展」，选择音乐源 → YouTube Music，保存。
该源与其他三个音源互斥；切换不会删除原有网易登录或洛雪配置。
歌曲带 `[YouTube Music]` 来源标记，仍在官方飞牛音乐中搜索和播放。

先通过 ytmusicapi 搜索 Music 歌曲。匿名曲库返回空结果时，用 yt-dlp 搜索
YouTube 视频的音轨；管理页与健康接口会显示 `youtube_audio_fallback`。
不提供冒充无损音质、虚构歌词或绕过账号权益的功能。
当前不实现 YouTube 的个人歌单同步，原有收藏仍由飞牛音乐管理。

## 登录

公开内容默认无需登录。管理页支持两种用途不同的认证：

OAuth：配置自己的 Google Cloud 客户端 ID 和密钥（类型为“电视和输入受限设备”），
启用 YouTube Data API v3，在管理页保存后点击开始授权，在 Google 官方页面输入设备码。
测试状态的 Google 应用需将自己的账号加入测试用户。页面自动轮询，支持到期、拒绝和限速。
只申请 youtube.readonly 权限，不写入或删除 Google 账号数据。
OAuth 用于 ytmusicapi 音乐账号接口，不为 yt-dlp 音频下载提供登录态。
OAuth 已授权时曲库优先用 OAuth，Cookie 仍独立用于播放；退出 OAuth 不删除 Cookie。
密钥和令牌位于 sources-data/youtube/oauth.json，权限 0600，令牌自动刷新并持久化。
页面不会回显密钥、访问令牌或刷新令牌。退出只删除本地令牌，Google 侧授权可在账号安全设置撤销。
Google 测试应用等限制可能使刷新令牌过期，此时须重新授权。

需要播放账号限制内容时，可导入专用 Netscape cookies.txt：

1. 使用独立无痕窗口登录 YouTube。
2. 在同一个标签页打开 `https://www.youtube.com/robots.txt`。
3. 导出仅包含 youtube.com 的 Cookie，关闭整个无痕窗口，不再打开该会话。
4. 在管理页点击导入；之后可以替换或清除。不要将文件上传 GitHub 或聊天。

服务器拒绝非 YouTube 域名、过期及缺少账户签名的 Cookie 文件。
会话保存在 `sources-data/youtube/cookies.txt`，目录 0700，文件 0600。
不收集 Google 密码，不将 Cookie 回显给前端或写入 .env、日志及安装包。
导入成功只表示文件已保存；实际有效性以搜索、播放为准。
Google 可能撤销会话，不能承诺永久登录。

yt-dlp 不支持 YouTube OAuth 播放认证，设备码授权不能替代播放所需的 Cookie。
如源站要求额外验证或 PO Token，应先更新维护组件或重新导入有效会话，
不要对外暴露 Google 账号或尝试关闭 TLS 校验。

## 网络与安全

`YOUTUBE_PROXY` 仅影响此音源，支持 HTTP(S) 和 SOCKS5。
留空使用 NAS 的正常出口，不修改 Windows 或 NAS 系统代理。
代理若含密码，会按敏感字段脱敏记录；配置仅供飞牛管理员访问。
YouTube 页面返回 HTTP 200 但提示地区不可用时，不能据此认为音源可用。

源服务在宿主机仅发布 `127.0.0.1:8776`；Docker 容器内为 8006。
会话管理还需由 WebUI 从数据卷读取的随机私钥；不会把密钥发给浏览器。
音频下载只允许 HTTPS 的 googlevideo.com 子域，逐跳校验重定向。
音频流支持 Range、过期签名刷新，并在客户端中断时关闭上游连接。
签名播放地址只作短时内存缓存；请求 ID 只能是 11 字符 YouTube 视频 ID。

## 维护和打包

YouTube 私有接口会变化，长期运行需要维护解析器，并不等同于永久可用。
依赖版本在 `youtube-service/requirements.txt` 明确锁定。
升级 yt-dlp / ytmusicapi 时，更新该文件，运行测试与真实播放检查后重新打包。
不配置无人值守更新，避免把未经检查的新依赖直接加载进生产应用。

```bash
python -m pip install -r proxy/requirements.txt -r webui-service/requirements.txt \
  -r youtube-service/requirements.txt pytest pytest-asyncio pillow PyYAML pytest-cov
python scripts/run_tests.py --jobs 2
bash packaging/fpk/build.sh
```

FPK 更新沿用原应用 ID `fnmusic-ext`。升级前的 `sources-data`、配置与收藏由
原生命周期脚本备份和恢复。升级时保留旧镜像标签及独立文件备份。
不要直接安装上游 FPK 覆盖本分支，否则会移除 YouTube 功能。

参考：
- https://github.com/yt-dlp/yt-dlp/wiki/Extractors
- https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html
