#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
临时解密插件 - 自动生成
"""

import json
import os

def setup(api):
    """插件初始化"""
    api.log("临时解密插件已加载")
    
    @api.on("on_ui_action")
    async def on_ui_action(params):
        if params.get("actionId") == "auto_decrypt":
            try:
                # 要解密的文件
                file_path = r"E:\new_project\jianying_jm\draft_content.json"
                output_path = r"E:\new_project\jianying_jm\draft_content_decrypted.json"
                
                api.log(f"正在解密: {file_path}")
                
                # 解密
                decrypted_content = api.read_draft_file(file_path)
                
                # 解析并格式化 JSON
                json_data = json.loads(decrypted_content)
                
                # 保存
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(json_data, f, ensure_ascii=False, indent=2)
                
                api.log(f"✓ 解密成功!")
                api.log(f"✓ 已保存到: {output_path}")
                
                # 显示结果
                api.alert(
                    f"解密成功!\n\n"
                    f"原文件: {os.path.basename(file_path)}\n"
                    f"输出: {os.path.basename(output_path)}\n"
                    f"大小: {len(decrypted_content)} 字节",
                    title="解密完成"
                )
                
            except Exception as e:
                api.log(f"✗ 解密失败: {e}")
                api.alert(f"解密失败:\n{str(e)}", title="错误")
    
    # 注册按钮
    api.register_ui_action(
        action_id="auto_decrypt",
        label="🔓 一键解密",
        icon="lock_open",
        location="home_quick_actions"
    )
    
    api.log("临时解密插件已就绪 - 点击 '🔓 一键解密' 按钮")
