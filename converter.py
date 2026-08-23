import subprocess
import sys
import os
import argparse
import json
import glob
import shutil

def get_max_duration(file_path):
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v',
        '-show_entries', 'stream=duration,nb_frames',
        '-of', 'json',
        file_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        max_duration = 0
        for stream in data.get('streams', []):
            if 'duration' in stream:
                dur = float(stream['duration'])
                if dur > max_duration:
                    max_duration = dur
        return max_duration if max_duration > 0 else None
    except Exception as e:
        print(f"⚠️ Не удалось получить длительность через ffprobe: {e}")
        return None

def convert_avif_to_webm(input_file, output_file, target_duration=3.0, action='none', quality_settings=None, strict_mode=False):
    if not os.path.exists(input_file):
        print(f"❌ Файл не найден: {input_file}")
        return False

    original_duration = get_max_duration(input_file)
    if not original_duration:
        print(f"⚠️ Не удалось определить длительность для {input_file}. Пропускаем.")
        return False

    if quality_settings is None:
        quality_settings = {'crf': 12, 'cpu_used': 2, 'deadline': 'good'}

    tolerance = 0.001 if strict_mode else 0.0
    safety_margin = 0.001
    effective_target = target_duration - safety_margin
    
    filters = '[0:2][0:3]alphamerge,format=yuva420p,scale=512:-2'
    duration_info = f"{original_duration:.3f}с"
    needs_processing = original_duration > (target_duration + tolerance)
    
    if action == 'compress' and needs_processing:
        multiplier = effective_target / original_duration
        filters += f',setpts=PTS*{multiplier}'
        duration_info += f" -> {effective_target:.3f}с (ускорено)"
    elif action == 'trim' and needs_processing:
        duration_info += f" -> {effective_target:.3f}с (обрезано)"
    elif needs_processing:
        duration_info += f" (⚠️ больше {target_duration:.3f}с, но действие не выбрано)"
    else:
        duration_info += f" (OK, ≤ {target_duration:.3f}с)"

    command = [
        'ffmpeg', '-i', input_file,
        '-filter_complex', filters,
        '-c:v', 'libvpx-vp9',
        '-pix_fmt', 'yuva420p',
        '-crf', str(quality_settings['crf']),
        '-b:v', '0',
        '-cpu-used', str(quality_settings['cpu_used']),
        '-deadline', quality_settings['deadline'],
        '-row-mt', '1',
        '-an', # <-- УДАЛЕНИЕ ЗВУКОВОЙ ДОРОЖКИ
        '-y',
    ]
    
    if action == 'trim' and needs_processing:
        command.extend(['-t', f'{effective_target:.3f}'])
    
    command.append(output_file)

    print(f"🔄 [{os.path.basename(input_file)}] -> [{os.path.basename(output_file)}]")
    print(f"     {duration_info}")
    print(f"     Качество: CRF={quality_settings['crf']}, CPU-used={quality_settings['cpu_used']}, Deadline={quality_settings['deadline']}")
    
    process = subprocess.run(command, capture_output=True, text=True)
    
    if process.returncode == 0:
        final_duration = get_max_duration(output_file)
        if final_duration and final_duration > (target_duration + tolerance):
            print(f"⚠️  Итоговая длительность {final_duration:.3f}с превышает лимит!")
        else:
            print(f"✅ Успешно! Итоговая длительность: {final_duration:.3f}с" if final_duration else "✅ Успешно!")
        return True
    else:
        print(f"❌ Ошибка FFmpeg:\n{process.stderr}")
        return False

def optimize_file_size(input_file, output_file, target_duration, action, quality_settings, strict_mode, target_size_kb=256, max_retries=3):
    original_duration = get_max_duration(input_file)
    if not original_duration:
        return False

    tolerance = 0.001 if strict_mode else 0.0
    safety_margin = 0.001
    effective_target = target_duration - safety_margin
    safety_size_kb = target_size_kb - 6 
    
    for attempt in range(1, max_retries + 1):
        target_bits = safety_size_kb * 1024 * 8
        target_bitrate = target_bits / original_duration
        
        if target_bitrate < 10000:
            target_bitrate = 10000

        filters = '[0:2][0:3]alphamerge,format=yuva420p,scale=512:-2'
        needs_processing = original_duration > (target_duration + tolerance)

        if action == 'compress' and needs_processing:
            multiplier = effective_target / original_duration
            filters += f',setpts=PTS*{multiplier}'

        command = [
            'ffmpeg', '-i', input_file,
            '-filter_complex', filters,
            '-c:v', 'libvpx-vp9',
            '-pix_fmt', 'yuva420p',
            '-b:v', str(int(target_bitrate)),
            '-minrate', str(int(target_bitrate * 0.9)),
            '-maxrate', str(int(target_bitrate)),
            '-bufsize', str(int(target_bitrate * 2)),
            '-cpu-used', str(quality_settings['cpu_used']),
            '-deadline', 'good',
            '-row-mt', '1',
            '-an', # <-- УДАЛЕНИЕ ЗВУКОВОЙ ДОРОЖКИ
            '-y',
        ]

        if action == 'trim' and needs_processing:
            command.extend(['-t', f'{effective_target:.3f}'])

        command.append(output_file)

        print(f"   🔄 Попытка оптимизации #{attempt} (битрейт: {int(target_bitrate/1000)} kbps, цель: {safety_size_kb} KB)...")
        process = subprocess.run(command, capture_output=True, text=True)
        
        if process.returncode != 0:
            print(f"   ❌ Ошибка при оптимизации:\n{process.stderr}")
            return False

        new_size_kb = os.path.getsize(output_file) / 1024
        print(f"   📊 Результат: {new_size_kb:.1f} KB")

        if new_size_kb <= target_size_kb:
            print(f"   ✅ Отлично! Файл уложился в лимит.")
            return True
        
        safety_size_kb = safety_size_kb * 0.9
        print(f"   ️ Всё ещё больше {target_size_kb} KB. Уменьшаем битрейт и пробуем снова...")

    print(f"   ⚠️ Не удалось уложиться в {target_size_kb} KB после {max_retries} попыток.")
    return False

def ask_action(original_duration, target_duration, auto_yes=False, strict_mode=False):
    tolerance = 0.001 if strict_mode else 0.0
    needs_processing = original_duration > (target_duration + tolerance)
    
    if not needs_processing:
        return 'none'
    if auto_yes:
        return 'trim'
    
    effective_target = target_duration - 0.001
    print(f"   ⚠️ Анимация длится {original_duration:.3f}с (больше {target_duration:.3f}с)")
    print(f"   [1] Обрезать до {effective_target:.3f}с")
    print(f"   [2] Ускорить до {effective_target:.3f}с")
    print(f"   [3] Оставить без изменений ({original_duration:.3f}с)")
    
    answer = input(f"   Ваш выбор [1/2/3, по умолчанию 1]: ").strip()
    if answer == '2': return 'compress'
    elif answer == '3': return 'none'
    else: return 'trim'

def format_size(size_bytes):
    size_kb = size_bytes / 1024.0
    color = "#e74c3c" if size_kb > 256 else "#2ecc71"
    return f"{size_kb:.1f} KB", color

def generate_index_html(output_dir, script_dir):
    webm_files = sorted(glob.glob(os.path.join(output_dir, '*.webm')))
    if not webm_files:
        print("⚠️ Нет .webm файлов для генерации index.html")
        return

    rel_path = os.path.relpath(output_dir, script_dir).replace('\\', '/')

    video_cards = ""
    for webm_path in webm_files:
        filename = os.path.basename(webm_path)
        video_src = f"{rel_path}/{filename}"
        size_text, size_color = format_size(os.path.getsize(webm_path))
        
        video_cards += f"""
        <div class="card">
            <video src="{video_src}" autoplay loop muted playsinline></video>
            <div class="filename">
                <div class="name">{filename}</div>
                <div class="size" style="color: {size_color}; font-weight: bold;">{size_text}</div>
            </div>
        </div>"""

    html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WebM Preview</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh; padding: 20px;
            background-color: #fff;
            background-image: linear-gradient(45deg, #ccc 25%, transparent 25%), linear-gradient(-45deg, #ccc 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #ccc 75%), linear-gradient(-45deg, transparent 75%, #ccc 75%);
            background-size: 20px 20px; background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
        }}
        h1 {{ text-align: center; margin-bottom: 30px; color: #333; background: rgba(255, 255, 255, 0.9); padding: 15px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 800px; margin-left: auto; margin-right: auto; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; max-width: 1400px; margin: 0 auto; }}
        .card {{ background: transparent; border: 1px solid rgba(0,0,0,0.1); border-radius: 10px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.1); transition: transform 0.2s; }}
        .card:hover {{ transform: translateY(-5px); }}
        video {{ width: 100%; height: auto; display: block; background: transparent; }}
        .filename {{ padding: 12px; text-align: center; background: rgba(255, 255, 255, 0.8); word-break: break-all; }}
        .name {{ font-size: 14px; color: #333; margin-bottom: 4px; }}
        .size {{ font-size: 13px; }}
        .info {{ text-align: center; margin-top: 30px; padding: 15px; background: rgba(255, 255, 255, 0.9); border-radius: 8px; color: #666; font-size: 14px; max-width: 600px; margin-left: auto; margin-right: auto; }}
    </style>
</head>
<body>
    <h1>🎬 WebM Preview ({len(webm_files)} файлов)</h1>
    <div class="grid">{video_cards}</div>
    <div class="info">🔍 Фон в шашечку показывает прозрачность. Если видно шашечку — альфа-канал работает!</div>
</body>
</html>"""

    index_path = os.path.join(script_dir, 'index.html')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    print(f"\n📄 Создан {index_path}")

def main():
    parser = argparse.ArgumentParser(description='Пакетная конвертация AVIF -> WebM')
    parser.add_argument('-i', '--input', required=True)
    parser.add_argument('-o', '--output', required=True)
    parser.add_argument('-t', '--time', type=float, default=3.0)
    parser.add_argument('-y', '--yes', action='store_true')
    parser.add_argument('--strict', action='store_true')
    parser.add_argument('--crf', type=int, default=12)
    parser.add_argument('--cpu-used', type=int, default=2)
    parser.add_argument('--deadline', choices=['best', 'good', 'realtime'], default='good')
    
    args = parser.parse_args()
    input_path, output_path = args.input, args.output
    target_time, auto_yes, strict_mode = args.time, args.yes, args.strict
    quality_settings = {'crf': args.crf, 'cpu_used': args.cpu_used, 'deadline': args.deadline}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    converted_files = []

    if os.path.isdir(input_path):
        os.makedirs(output_path, exist_ok=True)
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(input_path)), 'input_backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        avif_files = glob.glob(os.path.join(input_path, '*.avif'))
        if not avif_files:
            print(f"❌ В папке {input_path} не найдено файлов .avif"); sys.exit(1)
            
        print(f"📂 Найдено {len(avif_files)} файлов. Начинаем конвертацию...")
        
        for avif_file in avif_files:
            filename = os.path.basename(avif_file).replace('.avif', '.webm')
            out_file = os.path.join(output_path, filename)
            
            original_duration = get_max_duration(avif_file)
            action = ask_action(original_duration, target_time, auto_yes, strict_mode) if original_duration else 'none'
            
            if convert_avif_to_webm(avif_file, out_file, target_time, action, quality_settings, strict_mode):
                file_size_kb = os.path.getsize(out_file) / 1024
                if file_size_kb > 256:
                    print(f"   ⚠️ Размер {file_size_kb:.1f} KB > 256 KB. Запуск автооптимизации...")
                    optimize_file_size(avif_file, out_file, target_time, action, quality_settings, strict_mode)
                
                converted_files.append(out_file)
                shutil.move(avif_file, os.path.join(backup_dir, os.path.basename(avif_file)))
                print(f"   📦 Файл перемещён в {backup_dir}\n")
                
    elif os.path.isfile(input_path):
        if os.path.isdir(output_path):
            output_path = os.path.join(output_path, os.path.basename(input_path).replace('.avif', '.webm'))
        
        original_duration = get_max_duration(input_path)
        action = ask_action(original_duration, target_time, auto_yes, strict_mode) if original_duration else 'none'
        
        if convert_avif_to_webm(input_path, output_path, target_time, action, quality_settings, strict_mode):
            file_size_kb = os.path.getsize(output_path) / 1024
            if file_size_kb > 256:
                print(f"   ⚠️ Размер {file_size_kb:.1f} KB > 256 KB. Запуск автооптимизации...")
                optimize_file_size(input_path, output_path, target_time, action, quality_settings, strict_mode)
            converted_files.append(output_path)
    else:
        print(f"❌ {input_path} не является файлом или папкой."); sys.exit(1)

    if converted_files:
        video_dir = output_path if os.path.isdir(output_path) else os.path.dirname(output_path)
        generate_index_html(video_dir, script_dir)

if __name__ == "__main__":
    main()