/** 内置教程（D458）。第一版是短文字 + 操作按钮，后续可补截图/GIF。 */

export type TutorialCategory =
  | 'start'
  | 'chat'
  | 'voice'
  | 'visual'
  | 'space'
  | 'sync'
  | 'update'

export type TutorialAction =
  | 'open-models'
  | 'open-embedding'
  | 'open-voice'
  | 'open-live2d'
  | 'open-portraits'
  | 'open-sync'
  | 'open-mobile'
  | 'open-update'
  | 'create-friend'
  | 'resource'
  | 'restart-welcome'

export interface TutorialArticle {
  id: string
  category: TutorialCategory
  title: string
  summary: string
  body: string
  actions?: { label: string; action: TutorialAction }[]
}

export const TUTORIAL_CATEGORIES: { id: TutorialCategory; label: string }[] = [
  { id: 'start', label: '开始使用' },
  { id: 'chat', label: '聊天与模型' },
  { id: 'voice', label: '语音' },
  { id: 'visual', label: '立绘与 Live2D' },
  { id: 'space', label: '小世界' },
  { id: 'sync', label: '手机与同步' },
  { id: 'update', label: '更新与备份' },
]

export const TUTORIALS: TutorialArticle[] = [
  {
    id: 'start',
    category: 'start',
    title: '第一次使用：3 步开始',
    summary: '连模型、建朋友、说第一句话。',
    body: `1. 连接聊天模型
设置 → 模型 → 聊天，填服务地址、密钥、模型名，点「测试连接」。
任何 OpenAI 兼容服务都可以；Anthropic / Gemini 在「协议」里选。

2. 创建第一个朋友
点左上角「+ 创建朋友」，给她写名字和人格。人格写得越具体，她越像你描述的人。

3. 发第一句话
选中朋友，在下面输入框里说话就行。电脑端按 ↑ 可以翻回上一句。

以后想重新看这篇：右上角「?」→ 开始使用。`,
    actions: [
      { label: '去连接模型', action: 'open-models' },
      { label: '创建朋友', action: 'create-friend' },
    ],
  },
  {
    id: 'chat',
    category: 'chat',
    title: '聊天模型怎么填',
    summary: '地址、密钥、模型名分别是什么。',
    body: `「聊天」是唯一必须配的模型。其它视觉、语音、图片生成都是可选能力。

服务地址：服务商给你的 API 地址，通常以 /v1 结尾。
密钥：服务商后台生成的 API Key。已保存的密钥留空就是“不改”。
模型名：服务商文档里写的模型 ID，例如 deepseek-chat。

协议：
· OpenAI 兼容：大多数国内服务商、Ollama、自建网关
· Anthropic：Claude 的 Messages API
· Gemini：Google Gemini 原生 API

配好后点「测试连接」。失败时先检查地址、密钥、模型名和网络。`,
    actions: [{ label: '打开模型设置', action: 'open-models' }],
  },
  {
    id: 'voice',
    category: 'voice',
    title: '让她说话：语音设置',
    summary: '语音合成、音色库和每条回复自动语音。',
    body: `语音不是必须的，不开也能正常文字聊天。

设置 → 语音：
· 选语音合成引擎（本地 GPT-SoVITS 或云端）
· 填服务地址
· 在音色库里导入/选择音色
· 想让她每条回复都带语音，就在朋友的人格设置里打开「语音回复」

本地 GPT-SoVITS 第一次配置最麻烦；先用云端或系统语音试通，再换本地。`,
    actions: [{ label: '打开语音设置', action: 'open-voice' }],
  },
  {
    id: 'embedding',
    category: 'chat',
    title: '语义回忆：推荐模型还是本地文件',
    summary: '普通设置只开关；高级页再选模型来源。',
    body: `普通设置里「语义回忆」只有开 / 关。没有模型时，点开会在原地问你要不要下载推荐模型。

高级设置 → 语义检索，本地模型有两种来源：
· 推荐模型（自动下载）：Jina 中文量化模型，768 维，最省事
· 本地文件：选一个包含 model.onnx 和 tokenizer.json 的文件夹

API 模式支持 OpenAI 兼容 /embeddings 或 Gemini 原生 batchEmbedContents。
换模型后旧向量会过期，保存后点「重建索引」重算。`,
    actions: [{ label: '打开语义检索', action: 'open-embedding' }],
  },
  {
    id: 'visual',
    category: 'visual',
    title: '立绘与 Live2D',
    summary: '全局模型包，朋友里只选用哪个。',
    body: `立绘和 Live2D 都是全局资源，上传一次，所有朋友都能选。

立绘：
设置 → 立绘包 → 导入一套表情图；再在朋友的人格设置里选「立绘包」。

Live2D：
设置 → Live2D → 选模型 → 给情绪标签绑定参数/表情/动作 → 保存。
朋友的人格设置里选「Live2D」和具体模型。

静态立绘不会随机晃动；Live2D 会按模型设置做待机、表情过渡和说话口型。`,
    actions: [
      { label: '打开 Live2D', action: 'open-live2d' },
      { label: '打开立绘包', action: 'open-portraits' },
    ],
  },
  {
    id: 'space',
    category: 'space',
    title: '小世界怎么玩',
    summary: '3D 房间、触摸、换装和自由活动。',
    body: `选中朋友后点「小世界」进入 3D 房间。这里和普通聊天是分开的：
她可以自己走动、坐下、看书、玩偶，也可以对你的触摸做反应。

鼠标左键抓取/摸她，V 键切换穿墙，T 键在房间里说话。
小世界的对话默认不写进主聊天记录；需要她记住的事，她可以通过工具写记忆。

衣橱里可以换鞋和配饰；上下装始终穿着。`,
  },
  {
    id: 'sync',
    category: 'sync',
    title: '手机端与同步',
    summary: '让手机连上电脑，或手动同步数据。',
    body: `手机端是独立单机版，不连电脑也能用；同步是手动触发的。

设置 → 手机端：
· 电脑和手机在同一个网络，或用组网软件
· 扫描二维码，把地址填进手机 App
· 电脑端下载 APK 的入口也在这一页

设置 → 同步：
推送 / 拉取前先暂停自动交换。拉取会覆盖本机数据，先备份。
记忆也可以单独推送/拉取，不必覆盖整段聊天。`,
    actions: [
      { label: '打开手机端', action: 'open-mobile' },
      { label: '打开同步', action: 'open-sync' },
    ],
  },
  {
    id: 'update',
    category: 'update',
    title: '更新、备份与数据位置',
    summary: '自动更新、user_data 和手动备份。',
    body: `设置 → 更新：
· 启动时会自动检查新版本
· 「立即更新」会下载、校验、解压，然后重启替换程序
· 更新不会动 user_data/
· 失败会自动回滚旧版本；权限不足时用「跳转到资源页」

你的数据都在 exe 旁边的 user_data/：
聊天记录、记忆、语音、图片、Live2D/立绘、密钥设置。
换电脑或重装前，整个 user_data/ 复制走就是完整备份。

常见问题：
· 模型连不上：先看地址/密钥/模型名和「测试连接」的错误
· 没有语音：语音是可选能力，先去语音页配置
· 手机连不上：确认同一网络/组网软件，端口固定 8001
· 更新按钮灰的：源码版或安装目录不可写，用资源页下载`,
    actions: [
      { label: '打开更新页', action: 'open-update' },
      { label: '跳转资源页', action: 'resource' },
    ],
  },
]

export const TUTORIAL_BY_ID = Object.fromEntries(
  TUTORIALS.map((article) => [article.id, article]),
) as Record<string, TutorialArticle>
