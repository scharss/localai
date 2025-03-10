from flask import Flask, render_template, request, Response, stream_with_context
import requests
import json
import logging
import random
import time
import re
import markdown
import html

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG)

OLLAMA_API_URL = 'http://localhost:11434/api/generate'

# Emojis simplificados
THINKING_EMOJI = '🤔'
RESPONSE_EMOJI = '🤖'
ERROR_EMOJI = '⚠️'

def clean_math_expressions(text):
    """Limpia y formatea expresiones matemáticas."""
    # No eliminar los backslashes necesarios para LaTeX
    replacements = {
        r'\\begin\{align\*?\}': '',
        r'\\end\{align\*?\}': '',
        r'\\begin\{equation\*?\}': '',
        r'\\end\{equation\*?\}': '',
        r'\\ ': ' '  # Reemplazar \\ espacio con un espacio normal
    }
    
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
    
    return text

def format_math(text):
    """Formatea expresiones matemáticas para KaTeX."""
    def process_math_content(match):
        content = match.group(1).strip()
        content = clean_math_expressions(content)
        return f'$${content}$$'

    # Procesar comandos especiales de LaTeX antes de los bloques matemáticos
    text = re.sub(r'\\boxed\{\\text\{([^}]*)\}\}', r'<div class="boxed">\1</div>', text)
    text = re.sub(r'\\boxed\{([^}]*)\}', r'<div class="boxed">\1</div>', text)
    
    # Procesar bloques matemáticos inline y display
    text = re.sub(r'\$\$(.*?)\$\$', lambda m: f'$${m.group(1)}$$', text, flags=re.DOTALL)
    text = re.sub(r'\$(.*?)\$', lambda m: f'${m.group(1)}$', text)
    text = re.sub(r'\\\[(.*?)\\\]', process_math_content, text, flags=re.DOTALL)
    text = re.sub(r'\\\((.*?)\\\)', lambda m: f'${m.group(1)}$', text)
    
    # Preservar comandos LaTeX específicos
    text = re.sub(r'\\times(?![a-zA-Z])', r'\\times', text)  # Preservar \times
    text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'\\frac{\1}{\2}', text)  # Preservar fracciones
    text = re.sub(r'\\text\{([^}]*)\}', r'\1', text)  # Manejar \text correctamente
    
    return text

def format_code_blocks(text):
    """Formatea bloques de código con resaltado de sintaxis."""
    def replace_code_block(match):
        language = match.group(1) or 'plaintext'
        code = match.group(2).strip()
        return f'```{language}\n{code}\n```'

    # Procesar bloques de código
    text = re.sub(r'```(\w*)\n(.*?)```', replace_code_block, text, flags=re.DOTALL)
    return text

def format_response(text):
    """Formatea la respuesta completa con soporte para markdown, código y matemáticas."""
    # Primero formatear expresiones matemáticas
    text = format_math(text)
    
    # Formatear bloques de código
    text = format_code_blocks(text)
    
    # Convertir markdown a HTML preservando las expresiones matemáticas
    # Escapar temporalmente las expresiones matemáticas
    math_blocks = []
    def math_replace(match):
        math_blocks.append(match.group(0))
        return f'MATH_BLOCK_{len(math_blocks)-1}'

    # Guardar expresiones matemáticas
    text = re.sub(r'\$\$.*?\$\$|\$.*?\$', math_replace, text, flags=re.DOTALL)
    
    # Convertir markdown a HTML
    md = markdown.Markdown(extensions=['fenced_code', 'tables'])
    text = md.convert(text)
    
    # Restaurar expresiones matemáticas
    for i, block in enumerate(math_blocks):
        text = text.replace(f'MATH_BLOCK_{i}', block)
    
    # Limpiar y formatear el texto
    text = text.replace('</think>', '').replace('<think>', '')
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'([.!?])\s*([A-Z])', r'\1\n\2', text)
    
    return text.strip()

def decorate_message(message, is_error=False):
    """Decora el mensaje con emojis y formato apropiado."""
    emoji = ERROR_EMOJI if is_error else RESPONSE_EMOJI
    if is_error:
        return f"{emoji} {message}"
    
    formatted_message = format_response(message)
    return f"{emoji} {formatted_message}"

def get_thinking_message():
    """Genera un mensaje de 'pensando' aleatorio."""
    messages = [
        "Analizando tu pregunta...",
        "Procesando la información...",
        "Elaborando una respuesta...",
        "Pensando...",
        "Trabajando en ello...",
    ]
    return f"{THINKING_EMOJI} {random.choice(messages)}"

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message', '')
    model = data.get('model', 'deepseek-r1:7b')  # Obtener el modelo del request, con valor por defecto
    
    app.logger.debug(f"Mensaje recibido: {user_message}")
    app.logger.debug(f"Modelo seleccionado: {model}")

    def generate():
        try:
            # Enviar mensaje inicial de "pensando"
            thinking_msg = get_thinking_message()
            yield json.dumps({
                'thinking': thinking_msg
            }) + '\n'
            
            payload = {
                'model': model,  # Usar el modelo recibido
                'prompt': user_message,
                'stream': True
            }
            app.logger.debug(f"Enviando solicitud a Ollama API con payload: {payload}")
            
            response = requests.post(
                OLLAMA_API_URL,
                json=payload,
                stream=True,
                timeout=30
            )
            
            app.logger.debug(f"Estado de respuesta de Ollama API: {response.status_code}")
            if response.status_code != 200:
                error_msg = f"Error al conectar con Ollama API. Código de estado: {response.status_code}. Respuesta: {response.text}"
                app.logger.error(error_msg)
                yield json.dumps({
                    'error': decorate_message(error_msg, is_error=True)
                }) + '\n'
                return

            # Limpiar mensaje de "pensando" y comenzar a mostrar la respuesta
            yield json.dumps({'clear_thinking': True}) + '\n'
            
            # Inicializar acumulador de respuesta
            full_response = ""
            
            for line in response.iter_lines():
                if line:
                    try:
                        json_response = json.loads(line)
                        app.logger.debug(f"Fragmento de respuesta recibido: {json_response}")
                        ai_response = json_response.get('response', '')
                        if ai_response:
                            full_response += ai_response
                            # Formatear y enviar la respuesta completa hasta el momento
                            decorated_response = decorate_message(full_response)
                            yield json.dumps({'response': decorated_response}) + '\n'
                        
                    except json.JSONDecodeError as e:
                        app.logger.error(f"Error al decodificar JSON: {str(e)} para la línea: {line}")
                        continue

        except requests.exceptions.RequestException as e:
            error_msg = f"Error de conexión: {str(e)}"
            app.logger.error(error_msg)
            yield json.dumps({
                'error': decorate_message(error_msg, is_error=True)
            }) + '\n'

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/health', methods=['GET'])
def health_check():
    """Endpoint de verificación de salud"""
    status = {
        'status': 'healthy',
        'message': "Servidor en funcionamiento",
        'timestamp': time.time()
    }
    return json.dumps(status)

if __name__ == '__main__':
    app.logger.info("\n=== Servidor de Chat IA Iniciado ===")
    app.run(debug=True, port=5000) 