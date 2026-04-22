# ASO Split Pro — versão pronta para Render

## Requisitos
- Python 3.11
- Tesseract OCR com idioma português no servidor

## Como rodar localmente
```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python app.py
```

Abra:
`http://127.0.0.1:5000`

## Como publicar no Render
1. Suba estes arquivos para um repositório no GitHub.
2. No Render, crie um **Web Service** a partir do repositório.
3. O projeto já inclui `render.yaml`, `Procfile` e `runtime.txt`.
4. Depois do deploy, acesse a URL do serviço.

## Observações
- O sistema tenta extrair:
  - **Empresa** do campo `Empresa:`
  - **Funcionário** de `Funcionário:` ou `Nome:`
  - **CNPJ** quando houver 14 dígitos
- Se o CNPJ não existir, o nome sugerido sai sem ele.
- Em PDFs escaneados, a qualidade do OCR depende da nitidez da página.
