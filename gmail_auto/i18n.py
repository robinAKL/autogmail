"""The Chinese interface.

Pages are written in English. When the owner picks 中文, each HTML page is translated on its way out: text
between tags and a few attributes (placeholder, title, aria-label, data-confirm, data-busy) are looked up
in ZH, and sentences with names or numbers in them are matched by PATTERNS. Nothing the owner or their
mail wrote is touched: text areas, scripts, and anything inside an element marked translate="no" are left
as they are, and only whole pieces of interface text that match exactly are replaced.
"""

import html
import re

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_RAW = {"script", "style"}
_KEEP = {"textarea", "pre", "code"}
_ATTRS = re.compile(r'\b(placeholder|title|aria-label|data-confirm|data-busy|alt)="([^"]*)"')


def translate_page(page: str, language: str) -> str:
    if language != "zh" or not page:
        return page
    out: list[str] = []
    stack: list[tuple[str, bool]] = []
    skip = 0
    pos = 0
    while True:
        match = _TAG.search(page, pos)
        if match is None:
            out.append(_text_node(page[pos:], skip))
            break
        out.append(_text_node(page[pos : match.start()], skip))
        tag = match.group(0)
        pos = match.end()
        if tag.startswith("<!"):
            out.append(tag)
            continue
        head = _NAME.match(tag)
        name = (head.group(2) or "").lower() if head else ""
        closing = bool(head and head.group(1))
        if closing:
            out.append(tag)
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    skip -= sum(1 for _, hidden in stack[index:] if hidden)
                    del stack[index:]
                    break
            continue
        quiet = 'translate="no"' in tag
        out.append(tag if skip or quiet else _ATTRS.sub(_translate_attr, tag))
        if name in _RAW:
            # Scripts and styles are copied as they are, up to their closing tag.
            close = page.lower().find(f"</{name}", pos)
            close = len(page) if close == -1 else close
            out.append(page[pos:close])
            pos = close
            continue
        if name in _VOID or tag.endswith("/>"):
            continue
        hidden = quiet or name in _KEEP
        stack.append((name, hidden))
        if hidden:
            skip += 1
    return "".join(out)


_TAG = re.compile(r"<!--.*?-->|<![^>]*>|</?[A-Za-z][^>]*>", re.S)
_NAME = re.compile(r"<(/)?\s*([A-Za-z0-9:-]+)")


def _text_node(raw: str, skip: int) -> str:
    """Text between tags, translated when it is a known piece of interface text; otherwise left exactly as it was."""
    if skip or not raw or not re.search(r"[A-Za-z]", raw):
        return raw
    plain = html.unescape(raw)
    translated = _lookup(plain)
    return raw if translated == plain else html.escape(translated, quote=False)


def text(value: str, language: str = "zh") -> str:
    """One piece of interface text (a flash message, a label) in the chosen language."""
    if language != "zh":
        return value
    return _lookup(value)


def _lookup(value: str) -> str:
    key = " ".join((value or "").split())
    if not key or not re.search(r"[A-Za-z]", key):
        return value
    found = ZH.get(key)
    if found is None:
        for pattern, template in PATTERNS:
            match = pattern.fullmatch(key)
            if match:
                found = template.format(*[_lookup(group) if group else "" for group in match.groups()])
                break
    if found is None:
        return value
    lead = value[: len(value) - len(value.lstrip())]
    trail = value[len(value.rstrip()) :]
    return lead + found + trail


def _translate_attr(match: re.Match) -> str:
    value = html.unescape(match.group(2))
    return f'{match.group(1)}="{html.escape(_lookup(value), quote=True)}"'


# ---- the table -------------------------------------------------------------------------------
# Exact interface text → Chinese. Command words the owner types (YES, NO, LIST...), environment variable
# names, file names and product names stay as they are and are not listed.

ZH = {
    # menu and shell
    "Inbox": "收件箱", "Sent": "已发件", "Dashboard": "概览", "Contacts": "联系人", "Knowledge": "知识库", "Documents": "文档库",
    "Calendar": "日历", "Connections": "连接", "Settings": "设置", "Sign out": "退出登录", "This computer": "本机",
    "Mail Autopilot": "邮件自动驾驶", "· Mail Autopilot": "· 邮件自动驾驶", "Main": "主菜单",
    "Change how far replies can go": "修改回复可以自动发到什么程度", "Drafts waiting for your OK": "等你确认的草稿",
    "Write only": "只写草稿", "Show me every reply": "每封回复都给我看", "Send the easy ones": "简单的直接发",
    "Send whenever it can": "能发就发",
    "Drafts are saved. Nothing is sent from your mailbox.": "只保存草稿，不会从你的邮箱发出任何邮件。",
    "Every reply waits for your OK on WhatsApp or Approvals.": "每封回复都要你在 WhatsApp 或审核页面确认。",
    "If the knowledge base can answer without a new promise, it sends. Money, dates, missing facts wait for you.": "知识库能回答、又不用做新承诺的就直接发；涉及金额、日期或缺少信息的等你确认。",
    "Every reply goes out without asking. Use this only if you fully trust the knowledge base.": "所有回复都不经确认直接发出。只有完全信任知识库时才用。",
    # inbox
    "All": "全部", "To do": "待处理", "Notices": "提醒", "Replied": "已回复", "Skipped": "已跳过",
    "Write email": "写邮件", "Refresh list": "刷新列表", "Check mail now": "立即检查邮件", "Reading mailbox…": "正在读取邮箱…",
    "Checking mail…": "正在检查邮件…", "Mode": "模式", "Change": "修改", "Last check": "上次检查", "Not run yet": "还没运行过",
    "Connect a mailbox to begin": "先连接一个邮箱",
    "Mail Autopilot reads your unread mail, drafts replies from your profile and knowledge base, and asks you on WhatsApp when a reply needs your OK.": "邮件自动驾驶会读取你的未读邮件，按你的资料和知识库写回复草稿；需要你确认时，会在 WhatsApp 上问你。",
    "Open Connections": "打开连接页面", "No mailbox connected yet": "还没有连接邮箱",
    "This list comes from saved records. Opening a letter loads its text from the mailbox. Press Refresh list to load the newest mail.": "这个列表来自保存的记录。打开邮件时会从邮箱读取正文。点“刷新列表”加载最新邮件。",
    "Filter": "筛选", "Emails": "邮件", "Inbox pages": "收件箱分页", "Newer": "较新", "Older": "较早",
    "Nothing to do. New letters that need a reply show up here with a draft ready.": "没有待处理的邮件。需要回复的新邮件会带着写好的草稿出现在这里。",
    "No notices. Bills, fines, school and government letters land here when they arrive.": "没有提醒。账单、罚单、学校和政府来信到了会放在这里。",
    "Nothing here.": "这里没有邮件。", "Selected email": "选中的邮件", "Select an email to read it.": "选一封邮件来阅读。",
    "Sent mail": "已发邮件",
    "Letters sent from the mailboxes connected to this sign-in. Each one keeps the mailbox it came from.": "这里是当前登录账号已连接邮箱发出的邮件。每封都标着它来自哪个邮箱。",
    "Connect a mailbox to see mail you have sent.": "先连接一个邮箱，才能看到已发出的邮件。",
    "No sent mail in the connected mailboxes.": "已连接的邮箱里还没有已发邮件。",
    "What they want": "对方想要什么", "What to do": "该怎么办", "Open storyline": "打开故事线", "Add to calendar": "加到日历",
    "Adding to calendar…": "正在加到日历…", "Mark handled": "标记为已处理", "Not handled yet": "还没处理",
    "Paid, read or done: take it off Notices and To do": "已付款、已读或已办完：从“提醒”和“待处理”中移除",
    "Create quote": "生成报价单", "Create invoice": "生成发票", "Create contract": "生成合同", "Contract template": "合同模板",
    # seamless translation
    "Seamless Translation Settings": "无感翻译设置",
    "Read and write all foreign emails in your first language; translation happens seamlessly in the background.": "用你的母语阅读和撰写所有外语邮件，后台自动完成双向转译。",
    "First language (L1)": "母语 / 第一语言 (L1)",
    "Translation mode": "翻译模式",
    "Preserve languages": "免翻译语言",
    "These languages will stay in their original form without translation. Separate with commas.": "这些语言的邮件将保持原文呈现，不会被翻译。多个语言代码用逗号隔开。",
    "e.g. en, ja": "如: en, ja",
    "Auto-translate outgoing replies to recipient language": "发信时自动转译为对方语言",
    "Save translation settings": "保存翻译设置",
    "Seamless (view translated)": "无感翻译 (默认展示译文)",
    "Bilingual": "双语对照",
    "Off": "关闭",
    "Incoming letter (in your first language)": "对方来信 (母语译文)",
    "First language draft preview (How you understand it):": "母语草稿预览 (你的理解版本):",
    "Actual reply to send": "实际将发出的内容",
    "Filling in the quote…": "正在填写报价单…", "Filling in the invoice…": "正在填写发票…", "Filling in the contract…": "正在填写合同…",
    "Email content": "邮件内容",
    "This letter mentions passwords, codes or ID, so its text is not shown here. Open it in your mailbox.": "这封邮件提到了密码、验证码或证件，所以不在这里显示正文。请到邮箱里打开。",
    "Reply waiting for your OK": "等你确认的回复", "Why it is asking: ": "为什么要问你：", "Send reply": "发送回复",
    "Save changes": "保存修改", "Don't send": "不发送", "Send this reply now?": "现在发送这封回复吗？", "Sending…": "正在发送…",
    "Rewrite": "重写", "Rewriting…": "正在重写…", "What to change": "要改什么",
    "What to change, e.g. shorter, say we can do Friday 2pm": "要改什么，例如：短一点，说周五下午两点可以",
    "Reply that was sent": "已发出的回复", "Drafted reply": "回复草稿", "Written using": "参考了", "Reply": "回复",
    "Reply again": "再回复", "What to say (optional), e.g. yes, Friday 2pm works": "要说什么（可不填），例如：可以，周五下午两点",
    "What to say in the reply": "回复里要说什么", "Writing a draft…": "正在写草稿…",
    "Writes a draft from your knowledge base. Nothing is sent until you press Send.": "按你的知识库写草稿。你点“发送”之前不会发出任何东西。",
    "Storylines": "故事线", "Storyline": "故事线", "Open matters, summarised. Remove one when it is finished.": "进行中的事情汇总。办完了就移除。",
    "No open storylines. When a thread has more than one letter, it appears here.": "没有进行中的故事线。一个话题有多封邮件时会出现在这里。",
    "Close storyline": "关闭故事线", "Close": "关闭", "Timeline": "时间线", "Related mail": "相关邮件", "Status": "状态", "Using": "参考",
    "Remove from storyline": "从故事线移除", "Remove this matter from the storyline? The letters stay in Inbox.": "把这件事从故事线移除吗？邮件仍留在收件箱。",
    "Open contact": "打开联系人",
    "Sending is on. This round will really answer unread mail that has not been handled yet.": "发送已开启。这一轮会真的回复还没处理的未读邮件。",
    "Your profile is still the placeholder example. Drafts work, but real sending stays off until you": "你的资料还是示例内容。草稿可以写，但真正发送会一直关闭，直到你",
    "fill it in": "填写资料",
    # statuses
    "Replied automatically": "已自动回复", "Replied after your OK": "确认后已回复", "Draft saved": "已保存草稿",
    "Draft saved, needs a look": "已保存草稿，需要看一下", "Waiting for your OK": "等你确认", "Not sent: you declined": "未发送：你拒绝了",
    "Not sent: no answer in a week": "未发送：一周没回应", "Read, no reply": "已读，未回复", "Handled": "已处理",
    "Won't reply": "不回复",
    # skip reasons
    "Passwords, codes, ID or bank details": "含密码、验证码、证件或银行信息", "System or no-reply address": "系统或不回复地址",
    "Mailing list": "邮件列表", "Automatic reply": "自动回复", "Promotion": "推广", "Social notification": "社交通知",
    "Forum": "论坛", "From yourself": "你自己发的", "No text to answer": "没有可回复的内容", "No reply needed": "不需要回复",
    "Status notice, no reply needed": "状态通知，不需要回复", "Spam, ads or phishing": "垃圾、广告或钓鱼", "Phishing or scam": "钓鱼或诈骗",
    "Advertisement": "广告", "Could not write a reply": "没能写出回复", "Other": "其他",
    # approvals
    "Approvals": "待确认", "Replies and new emails that wait for your OK before they are sent.": "等你确认后才会发出的回复和新邮件。",
    "Write a new email": "写一封新邮件", "You are also asked on WhatsApp — answer in either place.": "WhatsApp 上也会问你，在哪边回答都可以。",
    "Connect WhatsApp": "连接 WhatsApp", "to be asked on your phone.": "，在手机上收到确认请求。", "New email": "新邮件",
    "Why it is asking": "为什么要问你", "Show the original letter": "显示原邮件", "Ask for a rewrite": "要求重写",
    "e.g. shorter, and say I can meet on Friday at 2pm": "例如：短一点，并说我周五下午两点可以见面", "(edited)": "（已修改）", ", edited": "，已修改",
    "To": "收件人", "Subject": "主题", "Email": "邮件", "Nothing is waiting for you": "没有等你确认的邮件",
    "When a reply would make a commitment, involves money or a decision, or needs a fact that is not in your knowledge base, it shows up here and on WhatsApp first.": "回复如果要做出承诺、涉及金额或决定，或者需要知识库里没有的信息，会先出现在这里和 WhatsApp 上。",
    "Recent decisions": "最近的处理", "Decided": "处理时间", "From": "发件人", "Where": "在哪里", "Outcome": "结果", "Sent": "已发送",
    "Not sent": "未发送", "to": "给",
    # attachments
    "Attach": "附加", "Attach a file from the knowledge base": "从知识库附加文件", "Attach files from this computer": "从电脑附加文件",
    "From the knowledge base…": "从知识库选择…", "Remove": "移除",
    # write page and compose
    "Write an email": "写邮件", "Simple form": "简单表单", "Conversation": "对话", "For example": "例如",
    "Write to Lee: confirm Wednesday 2pm, attach our company introduction": "给 Lee 写邮件：确认周三下午两点，附上公司介绍",
    "Make it shorter and more formal": "短一点，正式一点", "Also attach the price list": "再附上价目表",
    "Chat in Chinese or English. The email itself is written in the language chosen on the right.": "可以用中文或英文聊天，邮件本身按右边选的语言写。",
    "Tell the assistant who it is for, what to say and what to attach. It uses your contacts, documents and knowledge, asks when something is missing, and never sends on its own.": "告诉助手写给谁、说什么、附上什么。它会用你的联系人、文档和知识库，缺信息时先问你，不会自己发出去。",
    "No AI model is set, so the assistant cannot write. Type the email on the right.": "还没有设置 AI 模型，助手没法写。请在右边直接输入邮件。",
    "What should the email say, and what should go with it?": "邮件要说什么，附上什么？", "Message to the assistant": "给助手的消息",
    "Writing…": "正在写…", "Draft": "草稿", "Name or email address": "姓名或邮箱地址", "Email language": "邮件语言",
    "Change language": "切换语言", "Rewriting in the new language…": "正在用新语言重写…", "Saving…": "正在保存…",
    "Put in Approvals": "放进待确认", "Also waiting in Approvals. Changes here update it there.": "它也在待确认里。这里的修改会同步过去。",
    "Set up R2 file storage to attach files.": "要附加文件，请先设置 R2 文件存储。", "Set up R2 file storage to attach files": "要附加文件，请先设置 R2 文件存储",
    "Match the person": "按对方习惯", "English": "英文", "Chinese": "中文", "Chinese and English": "中英双语",
    "Say what it should cover.": "说一下要写什么内容。", "It is written from your knowledge base in your voice, then waits for you to check and send.": "会按你的语气、根据知识库来写，然后等你检查后发送。",
    "No AI model is set, so what you type is the email.": "还没有设置 AI 模型，你输入的内容就是邮件正文。",
    "Nothing is sent until you press Send.": "你点“发送”之前不会发出任何东西。", "Optional": "可不填", ": one is written for you": "：会帮你写一个",
    "What to say": "要说什么", "Quote or invoice": "报价单或发票", "None": "无", "Contract": "合同", "No contract": "不附合同",
    "From this computer": "从这台电脑", "Up to 18 MB a file.": "每个文件最大 18 MB。", "Write it": "写出来", "Save draft": "保存草稿",
    "Writing the email…": "正在写邮件…", "You check it on the next page before anything is sent.": "发出前会在下一页让你检查。",
    "lee@example.com (separate several with commas)": "lee@example.com（多个用逗号隔开）", "From the knowledge base": "从知识库",
    # contacts
    "Everyone you deal with by email, sorted into clients, students, suppliers and so on. Replies, new emails, invoices and contracts use what is here. Your edits are never overwritten.": "你通过邮件往来的所有人，按客户、学员、供应商等分类。回复、新邮件、发票和合同都会用到这里的信息。你改过的内容不会被覆盖。",
    "Scan mail": "扫描邮件", "Reading your mail…": "正在读取你的邮件…", "No contacts yet": "还没有联系人",
    "Press Scan mail. People who wrote to you (not robots or newsletters) and people you wrote to are added, and their type is suggested for you to confirm. New letters add people on their own after that.": "点“扫描邮件”。给你写过信的人（不含机器人和群发）和你写过信的人会被加进来，并建议一个类型让你确认。之后新邮件会自动添加联系人。",
    "Contact type": "联系人类型", "To confirm": "待确认", "Not sorted": "未分类", "not sorted": "未分类", "Search": "搜索",
    "Search contacts": "搜索联系人", "Search name, company or email": "搜索姓名、公司或邮箱", "Name": "姓名", "Company": "公司",
    "Type": "类型", "Last mail": "最近邮件", "Confirm": "确认", "Write": "写邮件", "No contacts here.": "这里没有联系人。", "suggested": "建议",
    "Client": "客户", "Student": "学员", "Supplier": "供应商", "Partner": "合作伙伴", "Friend": "朋友", "Family": "家人",
    "Institution": "机构", "Contact": "联系人",
    "Details": "详细信息", "Title": "职位", "Phone": "电话", "Greet them as": "称呼", "e.g. Hi Lee / Dear Mr Wong": "例如 Hi Lee / Dear Mr Wong",
    "Tone": "语气", "Language": "语言", "Address": "地址", "Other email addresses": "其他邮箱地址", "Separate with commas": "用逗号隔开",
    "Notes": "备注", "Anything replies should know, e.g. prefers calls in the morning": "回复时需要知道的事，例如：喜欢上午打电话",
    "Save": "保存", "Not a contact": "不是联系人", "Recent letters": "最近的邮件", "No handled letters from them yet.": "还没有处理过他们的邮件。",
    "Invoices": "发票", "Not set": "未设置", "Formal": "正式", "Casual": "随意",
    # documents, quotes, invoices, contracts
    "Everything you send to people: files you send as they are, and the quotes, invoices and contracts made from your templates. Any of them can go into an email.": "你发给别人的所有文件：原样发送的文件，以及用模板生成的报价单、发票和合同。都可以附到邮件里。",
    "Files to send": "可发送的文件", "Quotes": "报价单", "Contracts": "合同", "Templates": "模板", "Files you send as they are": "原样发送的文件",
    "File": "文件", "Added": "添加时间", "Send": "发送", "Download": "下载", "Not kept: upload again to attach it": "没有保存文件：重新上传后才能附加",
    "Nothing yet. Upload your company introduction, product or course decks, brochures and price list.": "还没有文件。上传你的公司介绍、产品或课程介绍、宣传册和价目表。",
    "Add files": "添加文件", "Files": "文件", "What are they?": "这是什么文件？", "Work it out for me": "自动判断", "Upload": "上传", "Uploading…": "正在上传…",
    "They also go into Knowledge, so replies can use what they say. Old invoices and contracts are kept as templates only, never as facts.": "文件也会进入知识库，回复时可以用到里面的内容。旧发票和旧合同只当模板用，不会当成事实。",
    "Set up R2 file storage to keep files for attaching on the hosted site.": "要在线上保存可附加的文件，请设置 R2 文件存储。",
    "Fill in your business details under Templates first. They go on every quote and invoice.": "请先在“模板”里填写公司信息，每张报价单和发票都会用到。",
    "Number": "编号", "For": "客户", "Date": "日期", "Due": "到期日", "Valid until": "有效期至", "valid until": "有效期至", "due": "到期",
    "Total": "合计", "Edit": "编辑", "Open": "打开", "invoiced": "已开发票",
    "No contracts yet. Open a letter and press Create contract.": "还没有合同。打开一封邮件，点“生成合同”。",
    "Quote and invoice details": "报价单和发票信息", "Not filled in yet.": "还没填写。", "Edit details": "编辑信息", "Contract templates": "合同模板",
    "None yet. Upload a contract you use (Word, PowerPoint, Excel or PDF) under Files to send, choosing Contract template.": "还没有。在“可发送的文件”里上传你用的合同（Word、PowerPoint、Excel 或 PDF），类型选“合同模板”。",
    "Company introduction": "公司介绍", "Product or service info": "产品或服务介绍", "Price list": "价目表", "Reference": "参考资料",
    "Invoice template": "发票模板", "Quote": "报价单", "Invoice": "发票",
    "Accepted: make the invoice": "客户已确认：生成发票", "Make a draft invoice from this quote?": "用这张报价单生成发票草稿吗？",
    "Turned into an invoice": "已转成发票", "Check every line and price. The number is given when you issue it.": "请核对每一行和价格。开出时才分配编号。",
    "Preview PDF": "预览 PDF", "Open PDF": "打开 PDF", "Could not be filled in:": "没能填上的内容：", "Bill to": "客户信息",
    "Empty: the day it is issued": "留空：按开出当天", "Lines": "明细", "Description": "描述", "Qty": "数量", "Quantity": "数量",
    "Unit price": "单价", "(incl. GST)": "（含 GST）", "Amount": "金额", "price?": "价格？", "Add a line": "添加一行",
    "Subtotal (excl. GST)": "小计（不含 GST）", "Amounts update when you save. Add more lines by saving; a fresh empty row appears each time.": "保存后金额会更新。要加更多行就先保存，每次都会多出一个空行。",
    "Issue and attach to reply": "开出并附到回复", "Issue": "开出", "Issuing…": "正在开出…", "Delete draft": "删除草稿",
    "Dates": "日期", "Attach to the reply": "附到回复", "Send in a new email": "用新邮件发送", "Open the letter": "打开原邮件",
    "Your details on every invoice": "每张发票上的你的信息", "Fill in from an old invoice": "从旧发票读取", "Read it": "读取",
    "Reading the invoice…": "正在读取发票…", "Business name": "公司名称", "Bank account": "银行账号", "Bank name": "银行名称",
    "GST number": "GST 号码", "GST rate (%)": "GST 税率（%）", "Number prefix": "编号前缀", "Next number": "下一个编号",
    "Pay within (days)": "付款期限（天）", "Currency": "币种", "GST registered (the PDF says Tax Invoice and adds GST)": "已注册 GST（PDF 显示 Tax Invoice 并加上 GST）",
    "My prices already include GST": "我的价格已含 GST", "Note at the bottom": "底部备注", "Save details": "保存信息",
    "Your invoices": "你的发票", "New blank invoice": "新建空白发票", "No invoices yet.": "还没有发票。",
    "Fill in your business details below first. They go on every invoice.": "请先在下面填写公司信息，每张发票都会用到。",
    "Open a letter and press Create invoice. The lines are filled from the letter and your knowledge base; amounts, GST and numbers are worked out for you.": "打开一封邮件，点“生成发票”。明细会按邮件和知识库填好，金额、GST 和编号由程序计算。",
    "Parts for this client": "这位客户要填的内容", "Part": "项目", "Value": "内容", "In the template": "模板里的写法", "not known yet": "还不知道",
    "An empty part shows as [its name] in the Word file, so nothing from the old client is left in.": "空着的项目在文件里会显示为 [项目名]，不会留下旧客户的信息。",
    "Attach to reply": "附到回复", "Attaching…": "正在附加…", "Delete this contract draft?": "删除这份合同草稿吗？", "Download file": "下载文件",
    "Drafts": "草稿", "Started": "开始时间", "Starting…": "正在开始…", "New blank contract": "新建空白合同", "No contracts drafted yet.": "还没有合同草稿。",
    "Your contract templates": "你的合同模板",
    # knowledge
    "Knowledge base": "知识库", "Memory": "记忆", "Sources": "来源", "Upload files": "上传文件", "Write a note": "写一条笔记",
    "Learn from past email": "从过去的邮件学习", "Reorganise knowledge": "整理知识库", "Reorganising knowledge…": "正在整理知识库…",
    "Rules that always win": "永远优先的规则", "Save rules": "保存规则", "Save note": "保存笔记", "Delete": "删除", "Cancel": "取消",
    "Add": "添加", "Add to knowledge": "加入知识库", "Add one…": "添加一条…", "Read again": "重新读取", "Read documents": "读取文档",
    "Reading files…": "正在读取文件…", "Reading your email…": "正在读取你的邮件…", "Reading SharePoint…": "正在读取 SharePoint…", "Reading…": "正在读取…",
    "Updating…": "正在更新…", "Choose files or drop them here": "选择文件或拖到这里", "Use this document as": "这份文档用作",
    "Used as a template only. Its client names and amounts are not used as facts in replies.": "只当模板用。里面的客户名和金额不会作为回复的事实。",
    "No documents yet": "还没有文档", "Source": "来源", "Last read": "上次读取", "Past": "过去", "past": "过去", "current": "当前",
    "No longer current": "不再有效", "Locked fact": "锁定的事实", "Knowledge map": "知识图谱", "See the map": "查看图谱",
    "Edit briefing": "编辑简介", "Save edits": "保存修改", "Briefing the model reads": "模型读取的简介", "Test what a reply would find": "测试回复会找到什么",
    "Nothing in the documents matches that.": "文档里没有匹配的内容。", "SharePoint sources": "SharePoint 来源", "Connected": "已连接",
    "Connect a mailbox first": "请先连接邮箱", "Go to sources": "去添加来源", "Review Memory": "查看记忆", "1. Add sources": "1. 添加来源",
    "2. Reorganise": "2. 整理", "3. Check Memory and Timeline": "3. 检查记忆和时间线", "Not organised yet.": "还没整理。",
    "A fact you want replies to use: prices, hours, policies.": "你希望回复用到的事实：价格、营业时间、政策等。",
    "Reads recent sent and received mail. Passwords and codes are left out.": "读取最近发出和收到的邮件，密码和验证码会被排除。",
    "Remove this source and the documents read from it?": "移除这个来源以及从它读取的文档吗？",
    "Remove this document from the knowledge base?": "从知识库移除这份文档吗？", "Summary": "摘要", "Open the original": "打开原文件",
    "Text that replies can draw on": "回复可以参考的文字", "e.g. invoice for the October workshop": "例如：十月工作坊的发票",
    # dashboard
    "History": "历史", "Yesterday": "昨天", "All time": "全部时间", "Time range": "时间范围", "Emails checked": "检查的邮件",
    "Replies sent": "已发送的回复", "Waiting for you now": "现在等你处理", "Writing time saved": "节省的写作时间",
    "about 4 minutes per reply": "每封回复约 4 分钟", "Review them": "去处理", "nothing to approve": "没有要确认的",
    "View as a table": "以表格查看", "Day": "日期", "Checked": "已检查", "Sent automatically": "自动发送", "Sent after your OK": "确认后发送",
    "Asked": "询问过", "Draft only": "只写草稿", "Failed": "失败", "No history yet": "还没有历史记录",
    "Go to the inbox": "去收件箱", "Your approvals": "你的确认", "Approved by you": "你确认的", "Drafts saved": "保存的草稿",
    "Typical reply time": "通常回复时间", "Time you take to decide": "你做决定的时间", "Why mail was skipped": "邮件被跳过的原因",
    "Who writes to you most": "谁给你写信最多", "When mail arrives": "邮件到达时间", "Emails by hour of day": "按小时统计的邮件",
    "By mailbox": "按邮箱", "Knowledge base at work": "知识库的使用情况", "Add documents": "添加文档", "New emails checked": "新检查的邮件",
    # settings
    "How far it sends": "自动发送到什么程度", "When to ping me": "什么时候提醒我", "Who it sounds like": "用谁的口吻", "Cleanup": "清理",
    "Sending mode": "发送模式", "Save sending mode": "保存发送模式", "Models": "模型", "Save models": "保存模型",
    "I understand that replies will really be sent from my mailbox.": "我知道回复会真的从我的邮箱发出。",
    "Reply language": "回复语言", "Save language": "保存语言", "Save profile": "保存资料", "Save reminders": "保存提醒设置",
    "Introduction": "简介", "Role": "身份", "Things I cannot promise": "我不能承诺的事", "Phrases I would say": "我常说的话",
    "Inbox cleanup": "收件箱清理", "Clean inbox now": "立即清理收件箱", "Cleaning inbox…": "正在清理收件箱…", "Save cleanup rules": "保存清理规则",
    "Settings sections": "设置分类", "Test Super": "测试 Super", "Test Worker": "测试 Worker", "Worker model": "Worker 模型",
    "Super agent model": "Super 模型", "Only the urgent ones": "只要紧急的", "Urgent now, the rest in the morning": "紧急的马上提醒，其余早上汇总",
    "I will pick the kinds": "我自己选类型", "Morning summary hour": "早上汇总的时间",
    "Send me yesterday's numbers on WhatsApp every morning": "每天早上在 WhatsApp 上发给我昨天的数据",
    "Tell me on WhatsApp when a reply was sent automatically": "自动发出回复时在 WhatsApp 上告诉我",
    "WhatsApp me important mail even when no draft is ready": "重要邮件即使没有草稿也在 WhatsApp 上提醒我",
    "WhatsApp me about calendar events 15 minutes before they start": "日历事项开始前 15 分钟在 WhatsApp 上提醒我",
    "Interface language": "界面语言",
    # connections
    "Mailboxes": "邮箱", "Connect Gmail": "连接 Gmail", "Connect Outlook": "连接 Outlook", "Connect SharePoint": "连接 SharePoint",
    "Connect again": "重新连接", "Disconnect": "断开", "Your number": "你的号码", "Send a test message": "发送测试消息",
    "Reaching you": "联系你的方式", "Knowledge sources": "知识来源", "Storage": "存储", "System": "系统", "Setup": "设置",
    "Incoming messages": "收到的消息", "Last webhook": "最近一次 webhook", "No WhatsApp webhook received yet.": "还没收到 WhatsApp 的 webhook。",
    "Open the knowledge base": "打开知识库", "AI agents": "AI 助手", "Sender account": "发件账号",
    # sign in
    "Sign in": "登录", "Register": "注册", "Code": "验证码", "6 digits": "6 位数字", "Email me a code to sign in": "把登录验证码发到我的邮箱",
    "Email me a code to register": "把注册验证码发到我的邮箱", "Verify and sign in": "验证并登录", "Verify and register": "验证并注册",
    "Use a different email": "换一个邮箱", "Your inbox, answered the way you would.": "你的收件箱，按你的方式回复。",
    "Works with Gmail and Outlook.": "支持 Gmail 和 Outlook。", "Replies are written from your own profile and knowledge base.": "回复按你自己的资料和知识库来写。",
    "Anything that needs a decision is sent to you on WhatsApp first.": "需要你做决定的事，会先发到 WhatsApp 问你。",
    "Your email is your username. A 6-digit code is sent to it and works for 10 minutes.": "你的邮箱就是用户名。我们会发一个 6 位验证码过去，10 分钟内有效。",
    # common flash messages
    "Draft ready. Check it, change anything you like, then send.": "草稿写好了。检查一下，想改就改，然后发送。",
    "Draft saved. It is still waiting for your OK.": "草稿已保存，仍在等你确认。", "The draft was rewritten. Check it, then send.": "草稿已重写。检查后再发送。",
    "Nothing was sent. The email stays in your inbox.": "没有发送，邮件仍留在收件箱。", "Attachment removed.": "附件已移除。",
    "Choose a file to attach first.": "请先选择要附加的文件。", "Marked as not handled yet.": "已标记为还没处理。",
    "Marked as handled. It is out of Notices and To do; find it under All.": "已标记为已处理。它已离开“提醒”和“待处理”，可以在“全部”里找到。",
    "Contact saved. Your changes are kept even when contacts are scanned again.": "联系人已保存。重新扫描也会保留你的修改。",
    "Type confirmed.": "类型已确认。", "Removed from contacts. It will not come back when mail is scanned.": "已从联系人中移除，重新扫描也不会再加回来。",
    "Contract saved.": "合同已保存。", "Contract draft deleted.": "合同草稿已删除。", "Invoice saved.": "发票已保存。", "Quote saved.": "报价单已保存。",
    "Draft invoice made from the quote. Check it, then issue it.": "已用报价单生成发票草稿。检查后再开出。",
    "Invoice details saved.": "发票信息已保存。", "Say what the email should do.": "请说一下这封邮件要写什么。",
    "That draft is no longer here. Start a new email.": "这份草稿已经不在了，请新写一封邮件。",
    "That email was already sent. Start a new one.": "这封邮件已经发出了，请新写一封。", "The email is empty.": "邮件是空的。",
    "Add who the email is to.": "请填写收件人。", "Interface language saved.": "界面语言已保存。",
    "invoice": "发票", "quote": "报价单",
    "Cc": "抄送", "Copy someone in (optional)": "抄送给谁（可不填）", "Copy someone in (separate several with commas)": "抄送给谁（多个用逗号隔开）",
    "Text only: upload the file again to attach it": "只有文字：重新上传文件后才能附上",
    "Attachments": "附件", "Choose from your files…": "从我的文件中选择…", "Upload a file": "上传文件",
    "Attach one of your files": "附上我的文件", "Quotes and invoices": "报价单和发票", "Still needed:": "还缺：",
    "Issue and attach": "开出并附上", "Issuing…": "正在开出…", "Draft quote": "报价单草稿", "Draft invoice": "发票草稿", "Draft contract": "合同草稿",
    "Give this quote its number and attach it?": "给这份报价单编号并附上吗？", "Give this invoice its number and attach it?": "给这张发票编号并附上吗？",
    "Or tell the assistant: \"attach our company introduction\", \"make a quote for 3 months\", \"add the service contract\".":
        "也可以直接跟助手说：“附上公司介绍”、“做一份 3 个月的报价”、“加上服务合同”。",
    "That is too much to attach to one email (15 MB in all).": "一封邮件附件太多了（总共最多 15 MB）。",
    "That quote or invoice was not started in this email.": "这份报价单或发票不是在这封邮件里起草的。",
    "Send me a morning report on WhatsApp: what needs me, mail from people, today's meetings, institutions and bookings": "每天早上在 WhatsApp 上发早报：需要我处理的事、真人来信、今天的会议、机构通知和预订",
    "Morning report time (New Zealand time)": "早报时间（新西兰时间）",
    "Include progress on my job applications (not job alerts)": "包括我的求职进展（不含职位推送）",
    "Insurance and policies": "保险和保单", "Policy schedules, renewals, claims and changes to your cover.": "保单附表、续保、理赔和保障变更。",
    "Accounts needing action": "账户需要操作",
    "A service asks you to verify, confirm or update something on your account, or a key or licence expired.": "某个服务要你验证、确认或更新账户信息，或者密钥、许可证过期了。",
    "Bookings and travel": "预订和行程", "Confirmed bookings, reservations, tickets and itineraries.": "已确认的预订、门票和行程。",
    "Job applications": "求职进展", "Progress on jobs you applied for: next steps, screening questions, interviews. Not job alerts.": "你申请的工作的进展：下一步、筛选问题、面试。不含职位推送。",
    # inbox, dashboard, write
    "Outlook is not connected yet. Open Connections and connect it.": "Outlook 还没连接。请打开“连接”页面去连接。",
    "Mail is checked every 5 minutes while the program is running.": "程序运行时每 5 分钟检查一次邮件。",
    "How your mail was handled yesterday, and over time.": "你的邮件昨天和一直以来是怎么处理的。",
    "Numbers appear here after the first round of mail has been checked.": "第一轮邮件检查完后，这里会显示数据。",
    "Say what it should cover. No AI model is set, so what you type is the email. Nothing is sent until you press Send.": "说一下要写什么内容。还没有设置 AI 模型，你输入的内容就是邮件正文。你点“发送”之前不会发出任何东西。",
    "Say what it should cover. It is written from your knowledge base in your voice, then waits for you to check and send. Nothing is sent until you press Send.": "说一下要写什么内容。会按你的语气、根据知识库来写，然后等你检查后发送。你点“发送”之前不会发出任何东西。",
    # knowledge
    "Sources become a map of current knowledge. The timeline keeps what used to be true. Replies use only what is still current.": "来源会整理成当前知识的图谱，时间线保留过去的情况。回复只使用当前有效的内容。",
    "Upload files, notes, SharePoint or a summary of past email. Write the rules that always win.": "上传文件、笔记、SharePoint 或过去邮件的摘要，并写下永远优先的规则。",
    "The assistant reads those sources and your settings, then draws facts, people, topics, rules and the rest.": "助手会读取这些来源和你的设置，整理出事实、人物、话题、规则等。",
    "Memory is current. Timeline is history. Facts cannot be rewritten — mark them no longer current, or reorganise so a new fact replaces the old one.": "记忆是当前的，时间线是历史。事实不能改写——可以标记为不再有效，或重新整理让新事实取代旧的。",
    "How it is connected": "它们之间的关系",
    "Current knowledge only — ended and replaced facts stay on the Timeline. Replies use what is still current.": "只显示当前知识——已结束或被取代的事实留在时间线上。回复只用当前有效的内容。",
    "sources": "来源", "No map yet": "还没有图谱", "Add a source or a rule, then choose": "添加一个来源或规则，然后点",
    ". The graph will show how facts, people, topics and rules hang together.": "。图谱会显示事实、人物、话题和规则之间的关系。",
    "Not organised yet. Only current items are used in replies.": "还没整理。回复只使用当前有效的内容。",
    "Upload files, summarise email history, or link SharePoint. Then reorganise so replies use those facts instead of guessing.": "上传文件、总结邮件历史，或连接 SharePoint，然后整理知识库，让回复用这些事实而不是靠猜。",
    "Facts": "事实", "People": "人物", "Topics": "话题", "Experience": "经验", "Preferences": "偏好", "Rules": "规则", "Decisions": "决定",
    "Nothing current": "没有当前内容", "Step 1 — what the map is built from": "第 1 步——图谱的来源",
    "Connect SharePoint to read a site, a library folder or a single file.": "连接 SharePoint 后，可以读取一个站点、一个文档库文件夹或单个文件。",
    "One per line. These are also cards under Rules, and they override everything else.": "每行一条。这些也会显示在“规则”卡片里，优先于其他所有内容。",
    # settings
    "Three questions: how far it may send, when to ping you, and whose voice it uses. Cleanup is mailbox hygiene, not replies.": "三个问题：可以自动发到什么程度、什么时候提醒你、用谁的口吻。清理是整理邮箱，不涉及回复。",
    "This is how far a reply may go without you. It does not decide what is important mail, and it does not change the knowledge base.": "这里决定不经过你时回复可以走多远。它不决定哪些邮件重要，也不改动知识库。",
    "Approvals show on the Approvals page.": "待确认的邮件显示在“待确认”页面。", "to be asked on your phone too.": "，也在手机上收到确认请求。",
    "Super decides. Worker reads, summarises and writes. This is advanced; most people can leave the defaults.": "Super 负责做决定，Worker 负责阅读、总结和写作。这是高级设置，大多数人用默认值就行。",
    "Super agent (decides what to do, reads WhatsApp intent)": "Super 助手（决定做什么，理解 WhatsApp 指令）",
    "Worker (reads, summarises, writes)": "Worker（阅读、总结、写作）",
    "Default (from environment: OPENAI_MODEL / gpt-4o-mini)": "默认（来自环境变量：OPENAI_MODEL / gpt-4o-mini）",
    "This is a phone reminder, not whether to reply. Replies follow sending mode and the knowledge base. Important mail still shows in Inbox.": "这里只管手机提醒，不决定要不要回复。回复按发送模式和知识库来。重要邮件仍会显示在收件箱。",
    "Add people and topics in": "在这里添加人物和话题：", "so “people you know” is yours, not a generic rule.": "，这样“你认识的人”就是你自己的，而不是通用规则。",
    "WhatsApp for fines, bills to pay, signatures, failed payments, school, government, health and home. Quotes and people you know stay in Inbox.": "罚单、要付的账单、签字、扣款失败、学校、政府、医疗和住房相关的邮件发到 WhatsApp。报价和认识的人留在收件箱。",
    "Same phone pings as above. Quotes, appointments and people you know show on Inbox Important and in the morning summary — not a ping for every letter.": "手机提醒同上。报价、约会和认识的人显示在收件箱和早上的汇总里——不会每封都提醒。",
    "Use the checklist below. This still only controls WhatsApp, not whether a reply is written.": "使用下面的清单。这里仍然只控制 WhatsApp 提醒，不决定是否写回复。",
    "Checked from top to bottom. Untick a kind to stop it reaching WhatsApp. It still shows in the inbox when it needs you.": "从上往下检查。取消勾选某一类，它就不会发到 WhatsApp，但需要你处理时仍会显示在收件箱。",
    "Fines and legal": "罚单和法律", "Infringement and parking/speed fines, tolls, final notices, debt collectors, court or summons.": "违章、停车或超速罚单、过路费、最后通知、催债、法院或传票。",
    "Bills to pay": "要付的账单", "Invoices, power/phone/rates bills, statements and anything overdue. Receipts for things already paid are not sent.": "发票、电费、电话费、地税账单、对账单和逾期款项。已付款的收据不会发送。",
    "Needs you to act": "需要你处理", "Documents to sign, failed or declined payments, customs fees, missed deliveries, renewals and expiring policies, registration or WOF due.": "要签字的文件、扣款失败或被拒、海关费用、错过的快递、续约和即将到期的保单、车辆注册或 WOF 到期。",
    "School and children": "学校和孩子", "Your children's schools and teachers.": "孩子的学校和老师。",
    "Government and institutions": "政府和机构", "IRD, ACC, NZTA, councils, police, courts, immigration, universities and other .govt.nz / .gov mail.": "IRD、ACC、NZTA、市政厅、警察、法院、移民局、大学以及其他 .govt.nz / .gov 邮件。",
    "Health": "医疗", "Test results, prescriptions, referrals and clinic or hospital mail.": "检查结果、处方、转诊以及诊所或医院的邮件。",
    "Home and tenancy": "住房和租赁", "Landlord, property manager, lease, rent, bond, body corporate, rates and inspections.": "房东、物业经理、租约、房租、押金、业主委员会、地税和房屋检查。",
    "Clients and enquiries": "客户和询价", "morning / inbox": "早上汇总 / 收件箱", "Quotes, pricing questions and new work from real people.": "真人发来的报价、价格询问和新工作。",
    "Appointments": "约会", "A real person inviting you to a meeting or appointment with a date that is still ahead.": "真人邀请你参加未来某天的会议或约会。",
    "People you know": "你认识的人", "Someone in your knowledge base or someone you have replied to before.": "知识库里的人，或你以前回复过的人。",
    "Your company": "你的公司", "Mail about your own company and the organisations you work with.": "关于你自己公司和合作机构的邮件。",
    "No-reply and notification senders, mailing lists and bulk mail": "不回复和通知类发件人、邮件列表和群发邮件",
    "Newsletters, marketing, sales, coupons and webinars": "新闻简报、营销、促销、优惠券和网络研讨会",
    "Receipts, order and delivery updates, sign-in and security alerts": "收据、订单和物流更新、登录和安全提醒",
    "Social media, forums, job alerts and product updates": "社交媒体、论坛、职位提醒和产品更新",
    "Who replies speak for": "回复代表谁", "Together with the": "和", "knowledge base": "知识库",
    ", this is the only source of facts. Replies also read the story of that matter (earlier letters). Facts that are not written down are never made up.": "一起，这是唯一的事实来源。回复也会参考这件事之前的往来邮件。没写下来的事实不会编造。",
    "Same language as the sender": "和对方用同一种语言", "Chinese, then English": "先中文后英文",
    "One per line. Experience, phone numbers, addresses and promises that are not written down are never made up.": "每行一条。没写下来的经历、电话、地址和承诺不会编造。",
    "One per line. Used to sound like you, not pasted into every reply.": "每行一条。用来模仿你的说话方式，不会原样贴进每封回复。",
    "One-click cleanup follows the rules you save here. Matching letters are deleted to Trash. Keep-list mail is never touched, even if a clean rule would match. This does not change how replies are written.": "一键清理按你在这里保存的规则执行，匹配的邮件会移到废纸篓。保留名单里的邮件永远不动，即使匹配了清理规则。这不影响回复怎么写。",
    "Clean if the letter matches": "邮件符合以下条件就清理", "One per line. Use": "每行一条。可以用",
    ", or any words found in the sender, subject or preview.": "，或者发件人、主题、预览里的任何词。",
    "Never clean if it matches": "符合以下条件永远不清理", "School mail, your companies, and anything else that must stay. Same matching as above.": "学校邮件、你的公司以及其他必须保留的邮件。匹配方式同上。",
    "Also delete no-reply, promotion, social and mailing-list mail the assistant would skip anyway": "同时删除助手本来就会跳过的不回复、促销、社交和邮件列表邮件",
    "Gmail is searched by these rules across Inbox (not only the newest page). Outlook looks at the latest 300 letters. Run it again if more matching mail remains.": "Gmail 会按这些规则搜索整个收件箱（不只是最新一页）。Outlook 查看最近 300 封。如果还有匹配的邮件，就再运行一次。",
    "Super:": "Super：", "· Worker:": "· Worker：", "· Using OpenAI direct (": "· 直接使用 OpenAI（", "set). Set": "已设置）。设置",
    "for Gemini and Claude.": "可以使用 Gemini 和 Claude。",
    # connections
    "Mailboxes to answer, sources to learn from, and the channel that reaches you.": "要回复的邮箱、要学习的来源，以及联系你的渠道。",
    "Not connected": "未连接", "Not set up": "未设置", "Available": "可用", "Key set": "已设置密钥", "Saving": "保存位置",
    "On this computer Gmail connects when the program starts: put the Desktop-app": "在本机上，Gmail 会在程序启动时连接：把桌面应用的",
    "in the folder and restart.": "放到文件夹里，然后重启。", "Needs a Microsoft app registration first. Set": "需要先注册一个 Microsoft 应用。设置",
    "and": "和", ", and register this redirect address:": "，并注册这个回调地址：",
    "When a reply needs your OK you get the email, the reason and the draft on WhatsApp. Answer": "回复需要你确认时，你会在 WhatsApp 上收到邮件、原因和草稿。回复",
    "to send,": "发送，", "to drop it, or just type what to change. You can also ask": "放弃，或者直接说要改什么。你也可以发",
    "Your WhatsApp number, with country code": "你的 WhatsApp 号码（含国家代码）",
    "Only this number is written to, and only messages from it are obeyed. Leave out the 0 after the country code (+64 21..., not +64 021...); it is removed for you if you type it.": "只会给这个号码发消息，也只听从这个号码的指令。国家代码后面不要加 0（写 +64 21...，不是 +64 021...）；如果加了会自动去掉。",
    "Set either": "设置以下任一种：", "(Meta WhatsApp Cloud API), or": "（Meta WhatsApp Cloud API），或", "(Twilio).": "（Twilio）。",
    "Save it on the left.": "在左边保存。", "Point the provider's webhook at:": "把服务商的 webhook 指向：",
    "This address is only reachable on this computer. To answer from your phone, run a tunnel (for example ngrok) to port 8765 and use its https address, or use the hosted site.": "这个地址只能在本机访问。要从手机回复，请运行隧道（例如 ngrok）到 8765 端口并使用它的 https 地址，或者使用线上网站。",
    "Meta also needs": "Meta 还需要", "Uses the same Microsoft app registration as Outlook: set": "和 Outlook 用同一个 Microsoft 应用注册：设置",
    "Files and email history": "文件和邮件历史", "Upload documents, write notes, or let it summarise your past email. No connection needed.": "上传文档、写笔记，或让它总结你过去的邮件。不需要连接。",
    "Super (decides) uses": "Super（做决定）使用", ". Worker (reads and writes) uses": "。Worker（阅读和写作）使用",
    "Knowledge, approvals and history are kept in: This computer (data folder).": "知识库、待确认和历史记录保存在：本机（data 文件夹）。",
    # calendar
    "Today": "今天", "Nothing on this day.": "这天没有安排。", "From email": "来自邮件",
    "Gmail could not be read. The connection is still saved.": "Gmail 这次没读出来。连接还在。",
    "Outlook could not be read. The connection is still saved.": "Outlook 这次没读出来。连接还在。",
    "Pacific/Auckland · Gmail and Outlook": "奥克兰时间 · Gmail 和 Outlook",
    "A letter from a real person that names a meeting or appointment with a date still ahead can be added automatically. Notifications, newsletters and letters without a date are never added. You can also add the opened letter from the inbox.": "真人来信里提到未来日期的会议或约会，可以自动加到日历。通知、新闻简报和没有日期的邮件不会加。你也可以在收件箱里把打开的邮件加到日历。",
    "Add dated meetings and appointments from mail to this calendar": "把邮件里带日期的会议和约会加到这个日历",
    "Mon": "周一", "Tue": "周二", "Wed": "周三", "Thu": "周四", "Fri": "周五", "Sat": "周六", "Sun": "周日",
}

# Sentences with a name, number or file in them. The groups are translated too when they are themselves listed.
_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
MONTH_ZH = {name: f"{index} 月" for index, name in enumerate(_MONTHS.split("|"), 1)}
DAY_ZH = dict(zip(_DAYS.split("|"), ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")))
ZH.update(MONTH_ZH)
ZH.update(DAY_ZH)

PATTERNS = [
    (re.compile(r"(.+) · Mail Autopilot"), "{0} · 邮件自动驾驶"),
    (re.compile(rf"({_MONTHS}) (\d{{4}})"), "{1} 年 {0}"),
    (re.compile(rf"({_DAYS}) (\d{{1,2}}) ({_MONTHS})"), "{2} {1} 日 {0}"),
    (re.compile(r"([a-z]+(?:, [a-z]+)+)\. Up to 18 MB each to be attachable\."), "{0}。每个文件最大 18 MB 才能附加。"),
    (re.compile(r"([a-z]+(?:, [a-z]+)+)\. Each file under (\d+) MB — the hosted site rejects anything larger\. Several smaller files are sent one by one\."), "{0}。每个文件小于 {1} MB——线上网站会拒绝更大的文件。多个小文件会逐个上传。"),
    (re.compile(r"Upload one you sent before \(([a-z, ]+)\)\. Your name, address, GST and bank details, and the next number are read from it\."), "上传一张你以前开过的发票（{0}）。会从里面读出你的名称、地址、GST、银行信息和下一个编号。"),
    (re.compile(r"(\d+) kept · ([\d,]+) characters"), "已保存 {0} 份 · {1} 个字符"),
    (re.compile(r"Fine-tune kinds \((\d+)\)"), "细调类型（{0}）"),
    (re.compile(r"Never sent to WhatsApp \((\d+)\)"), "从不发到 WhatsApp（{0}）"),
    (re.compile(r"Skipped: (.+)"), "已跳过：{0}"),
    (re.compile(r"Won't reply: (.+)"), "不回复：{0}"),
    (re.compile(r"Handled (\d{4}-\d{2}-\d{2})"), "已处理 {0}"),
    (re.compile(r"Added (\d+) files? to the knowledge base\."), "已向知识库添加 {0} 个文件。"),
    (re.compile(r"Reply sent to (.+)\."), "回复已发送给 {0}。"),
    (re.compile(r"Sent to (.+)\."), "已发送给 {0}。"),
    (re.compile(r"Attached (.+)\. Check the reply, then send\."), "已附加 {0}。检查回复后再发送。"),
    (re.compile(r"The email to (.+) is waiting in Approvals\. Send it from there, or on WhatsApp\."), "发给 {0} 的邮件已放进待确认，可以在那里或 WhatsApp 上发送。"),
    (re.compile(r"Email to (.+) is written\. Check it, then send\."), "给 {0} 的邮件写好了。检查后再发送。"),
    (re.compile(r"(Invoice|Quote) (\S+) issued\. Download it, or attach it to a reply\."), "{0} {1} 已开出。可以下载，或附到回复里。"),
    (re.compile(r"(Invoice|Quote) (\S+) is attached to the reply\. Check it, then send\."), "{0} {1} 已附到回复。检查后再发送。"),
    (re.compile(r"Draft (invoice|quote) deleted\."), "草稿已删除。"),
    (re.compile(r"(\d+) new and (\d+) updated from your mail; (\d+) contacts in all\.(.*)"), "从邮件里新增 {0} 个、更新 {1} 个联系人，共 {2} 个。{3}"),
    (re.compile(r"Sorted (\d+): check the suggested types\."), "已分类 {0} 个，请检查建议的类型。"),
    (re.compile(r"Not an email address: (.+)\."), "不是有效的邮箱地址：{0}。"),
    (re.compile(r"(\d+) waiting for you:"), "{0} 封等你处理："),
    (re.compile(r"(\d+) letters from them, (\d+) from you"), "他们来信 {0} 封，你发出 {1} 封"),
    (re.compile(r"Started (.+)\. Check every value; the rest of the contract is your template, unchanged\."), "开始于 {0}。请核对每一项内容，合同其余部分保持你的模板原样。"),
    (re.compile(r"Still empty: (.+)\."), "还没填：{0}。"),
    (re.compile(r"Before it can be issued: (.+)\."), "开出前还需要：{0}。"),
    (re.compile(r"Draft (invoice|quote)"), "{0}草稿"),
    (re.compile(r"New blank (invoice|quote)"), "新建空白{0}"),
    (re.compile(r"(Invoice|Quote) (\S+)"), "{0} {1}"),
]
