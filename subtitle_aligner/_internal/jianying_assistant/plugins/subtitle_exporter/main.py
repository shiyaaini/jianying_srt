import os
import json
import re
import asyncio

class SubtitleItem:
    def __init__(self, index, start_time, end_time, text, font_size=None, color=None, pos_x=None, pos_y=None):
        self.index = index
        self.start_time = start_time  # ms
        self.end_time = end_time      # ms
        self.text = text
        self.font_size = font_size
        self.color = color
        self.pos_x = pos_x
        self.pos_y = pos_y

    def format_time_srt(self, ms):
        hours = ms // 3600000
        minutes = (ms % 3600000) // 60000
        seconds = (ms % 60000) // 1000
        milli = ms % 1000
        return f"{hours:02}:{minutes:02}:{seconds:02},{milli:03}"

    def format_time_ass(self, ms):
        hours = ms // 3600000
        minutes = (ms % 3600000) // 60000
        seconds = (ms % 60000) // 1000
        centi = (ms % 1000) // 10
        return f"{hours}:{minutes:02}:{seconds:02}.{centi:02}"

    def to_srt_entry(self):
        start = self.format_time_srt(self.start_time)
        end = self.format_time_srt(self.end_time)
        return f"{self.index}\n{start} --> {end}\n{self.text}\n"

    def to_ass_dialogue(self):
        pos_tag = ""
        if self.pos_x is not None and self.pos_y is not None:
            x = int(self.pos_x * 1920)
            y = int(self.pos_y * 1080)
            pos_tag = f"{{\\pos({x},{y})}}"

        start = self.format_time_ass(self.start_time)
        end = self.format_time_ass(self.end_time)
        return f"Dialogue: 0,{start},{end},Default,,0,0,0,,{pos_tag}{self.text}"

def convert_to_ass_color(hex_color):
    if not hex_color: return None
    clean_hex = hex_color.replace('#', '')
    if len(clean_hex) == 6:
        r, g, b = clean_hex[0:2], clean_hex[2:4], clean_hex[4:6]
        return f"&H00{b}{g}{r}"
    elif len(clean_hex) == 8:
        r, g, b, a = clean_hex[0:2], clean_hex[2:4], clean_hex[4:6], clean_hex[6:8]
        return f"&H{a}{b}{g}{r}"
    return None

def extract_text(content):
    if not content: return ""
    if isinstance(content, dict):
        return content.get('text', '')
    if isinstance(content, str):
        if content.startswith('{'):
            try:
                data = json.loads(content)
                return data.get('text', '')
            except: pass
        return content
    return str(content)

def parse_subtitles(draft_json, draft_name):
    subtitles = []
    tracks = draft_json.get('tracks', [])
    materials = draft_json.get('materials', {})
    texts = materials.get('texts', [])

    # Method 1 & 2 combined
    for track in tracks:
        track_type = track.get('type', '')
        if track_type in ['text', 'subtitle']:
            segments = track.get('segments', [])
            for segment in segments:
                material_id = segment.get('material_id')
                text_material = next((t for t in texts if t.get('id') == material_id), None)

                timerange = segment.get('target_timerange')
                if not timerange: continue

                start_us = timerange.get('start', 0)
                duration_us = timerange.get('duration', 0)
                start_ms = start_us // 1000
                end_ms = (start_us + duration_us) // 1000

                text = extract_text(segment.get('content'))
                if not text and text_material:
                    text = extract_text(text_material.get('content'))

                if not text: continue

                pos_x, pos_y = None, None
                clip = segment.get('clip')
                if clip and 'transform' in clip:
                    trans = clip['transform']
                    pos_x = trans.get('x')
                    pos_y = trans.get('y')
                    if pos_x is not None: pos_x = (pos_x + 1) / 2
                    if pos_y is not None: pos_y = (1 - pos_y) / 2

                color, font_size = None, None
                if text_material:
                    font_size = text_material.get('font_size')
                    hex_color = text_material.get('text_color')
                    color = convert_to_ass_color(hex_color)

                subtitles.append(SubtitleItem(
                    len(subtitles) + 1, start_ms, end_ms, text.strip(),
                    font_size=font_size, color=color, pos_x=pos_x, pos_y=pos_y
                ))

    # Method 3: config.subtitle_taskinfo
    if not subtitles:
        subtitle_taskinfo = draft_json.get('config', {}).get('subtitle_taskinfo', [])
        for task in subtitle_taskinfo:
            content_str = task.get('content', '')
            if not content_str: continue
            try:
                content_json = json.loads(content_str)
                utterances = content_json.get('utterances', [])
                for utt in utterances:
                    text = utt.get('text', '')
                    if text:
                        subtitles.append(SubtitleItem(
                            len(subtitles) + 1,
                            utt.get('start_time', 0),
                            utt.get('end_time', 0),
                            text
                        ))
            except: pass

    # Method 4: extra_info.subtitle_fragment_info_list
    if not subtitles:
        fragment_list = draft_json.get('extra_info', {}).get('subtitle_fragment_info_list', [])
        for frag in fragment_list:
            cache_info_str = frag.get('subtitle_cache_info', '')
            if not cache_info_str: continue
            try:
                cache_info = json.loads(cache_info_str)
                sentence_list = cache_info.get('sentence_list', [])
                for sent in sentence_list:
                    text = sent.get('text', '')
                    if text:
                        subtitles.append(SubtitleItem(
                            len(subtitles) + 1,
                            sent.get('start_time', 0),
                            sent.get('end_time', 0),
                            text
                        ))
            except: pass

    subtitles.sort(key=lambda x: x.start_time)
    for i, item in enumerate(subtitles):
        item.index = i + 1

    return subtitles

def generate_srt(subtitles):
    return "".join(item.to_srt_entry() for item in subtitles)

def generate_ass(subtitles, draft_name, width=1920, height=1080):
    primary_color = "&H00FFFFFF"
    font_size = 60.0
    if subtitles:
        if subtitles[0].color: primary_color = subtitles[0].color
        if subtitles[0].font_size: font_size = subtitles[0].font_size * 10

    lines = [
        "[Script Info]",
        f"Title: {draft_name}",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColor, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,{font_size},{primary_color},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    for item in subtitles:
        lines.append(item.to_ass_dialogue())
    return "\n".join(lines)

def setup(api):
    api.log("字幕导出工具插件已初始化")

    @api.on("on_ui_action")
    async def on_ui_action(params):
        if params.get("actionId") == "export_subtitle_plugin":
            selected_drafts = params.get("params", {}).get("selectedDrafts", [])
            if not selected_drafts:
                api.alert("请先选择至少一个草稿")
                return 

            output_dir = api.select_directory("选择字幕导出目录")
            if not output_dir:
                return

            success_count = 0
            for draft_info in selected_drafts:
                try:
                    name = draft_info.get('name', '未命名草稿')
                    folder_path = draft_info.get('folderPath')
                    draft_file = os.path.join(folder_path, "draft_content.json")

                    if not os.path.exists(draft_file):
                        draft_file = os.path.join(folder_path, "draft_meta_info.json")
                        if not os.path.exists(draft_file):
                            api.log(f"跳过 {name}: 找不到草稿文件")
                            continue

                    # 读取草稿内容
                    content = api.read_draft_file(draft_file)
                    try:
                        draft_json = json.loads(content)
                    except:
                        api.log(f"解析 {name} 失败: JSON 格式错误")
                        continue

                    subtitles = parse_subtitles(draft_json, name)
                    if not subtitles:
                        api.log(f"草稿 {name} 未发现字幕内容")
                        continue

                    # 生成文件名
                    safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)
                    srt_path = os.path.join(output_dir, f"{safe_name}.srt")
                    ass_path = os.path.join(output_dir, f"{safe_name}.ass")

                    # 写入文件
                    with open(srt_path, "w", encoding="utf-8") as f:
                        f.write(generate_srt(subtitles))
                    with open(ass_path, "w", encoding="utf-8") as f:
                        f.write(generate_ass(subtitles, name))

                    success_count += 1
                    api.log(f"已导出 {name} 的字幕")

                except Exception as e:
                    api.log(f"处理 {draft_info.get('name')} 时出错: {str(e)}")

            if success_count > 0:
                api.show_notification(f"字幕导出完成：成功处理 {success_count} 个项目", title="导出完成", type="success")
                api.alert(f"字幕导出完成！\n成功：{success_count} 个\n保存目录：{output_dir}")
                return
            else:
                api.alert("未成功导出任何字幕，请检查草稿是否包含字幕。")
                return

    api.register_ui_action(
        action_id="export_subtitle_plugin",
        label="提取字幕",
        icon="magic",
        location="draft_action_bar"
    )