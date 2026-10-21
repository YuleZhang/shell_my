"""Offline browser regression check; all API calls are fixture-only, driver downloads disabled."""
import json
import os
from pathlib import Path
from time import sleep
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

os.environ['SE_OFFLINE'] = 'true'
options = Options()
options.add_argument('-headless')
options.set_preference('network.proxy.type', 1)
options.set_preference('network.proxy.http', '127.0.0.1')
options.set_preference('network.proxy.http_port', 9)
options.set_preference('network.proxy.ssl', '127.0.0.1')
options.set_preference('network.proxy.ssl_port', 9)
options.set_preference('network.proxy.no_proxies_on', '127.0.0.1,localhost')
options.set_preference('app.update.enabled', False)
options.set_preference('browser.safebrowsing.malware.enabled', False)
options.set_preference('browser.safebrowsing.phishing.enabled', False)
options.set_preference('network.captive-portal-service.enabled', False)
options.set_preference('network.connectivity-service.enabled', False)
options.set_preference('datareporting.healthreport.uploadEnabled', False)
import shutil

geckodriver = os.environ.get('EH_GECKODRIVER') or shutil.which('geckodriver')
if not geckodriver:
    raise RuntimeError('Install geckodriver or set EH_GECKODRIVER; downloads are disabled')
driver = webdriver.Firefox(options=options, service=Service(geckodriver, log_output=str(Path(__file__).with_name('geckodriver.log'))))
wait = WebDriverWait(driver, 15)
base = os.environ.get('EH_BROWSER_FIXTURE_URL', 'http://127.0.0.1:5177/tests/fixture.html')

def click(text, root=None):
    scope = root or driver
    button = wait.until(lambda _: next((b for b in scope.find_elements(By.TAG_NAME, 'button') if b.text == text and b.is_enabled()), None))
    driver.execute_script('arguments[0].click()', button)

def calls():
    return driver.execute_script('return window.fixture.calls')

def article(job_id):
    return next(a for a in driver.find_elements(By.CSS_SELECTOR, '.job-history-item') if job_id in a.text)

def open_report():
    click('打开报告', article('draft'))
    wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, 'textarea:not([readonly]).markdown-editor'))

try:
    driver.set_window_size(1280, 1000)
    driver.get(base)
    wait.until(lambda _: len(driver.find_elements(By.CSS_SELECTOR, '.job-history-item')) == 4)
    assert all(c['method'] == 'GET' for c in calls())
    click('查看任务状态', article('recover'))
    sleep(2)
    assert len([c for c in calls() if c['path'] == '/api/jobs/recover']) == 1
    assert all(c['method'] == 'GET' for c in calls())
    click('重新确认并恢复生成', article('recover'))
    dialog = wait.until(lambda _: driver.find_element(By.CSS_SELECTOR, 'dialog[open]'))
    wait.until(lambda _: '2345 字符' in dialog.text)
    resume_button = next(b for b in dialog.find_elements(By.TAG_NAME, 'button') if b.text == '承担调用费用，恢复此生成任务')
    driver.save_screenshot(str(Path(__file__).with_name('resume-dialog.png')))
    assert not resume_button.is_enabled()
    assert 'Original model' in dialog.text
    driver.execute_script('arguments[0].click()', dialog.find_element(By.CSS_SELECTOR, 'input[type=checkbox]'))
    click('承担调用费用，恢复此生成任务', dialog)
    wait.until(lambda _: not driver.find_elements(By.CSS_SELECTOR, 'dialog[open]'))
    body = next(c['body'] for c in calls() if c['method'] == 'POST')
    assert body == {'kind': 'generate', 'confirmResume': True, 'modelId': 'original', 'accountId': 'bound', 'maxOutputTokens': 3000,
                    'idempotencyKey': 'original-key', 'confirmedInputChars': 2345, 'confirmBudget': True, 'retryUnknown': False}
    click('查看任务状态', article('collect'))
    sleep(2)
    assert len([c for c in calls() if c['path'] == '/api/jobs/collect']) == 1
    click('明确恢复此采集任务', article('collect'))
    wait.until(lambda _: any(c['path'] == '/api/jobs/collect/resume' for c in calls()))
    assert next(c['body'] for c in calls() if c['path'] == '/api/jobs/collect/resume') == {'kind': 'collect', 'confirmResume': True}
    wait.until(lambda _: any(c['path'] == '/api/jobs/collect' for c in calls()[len(calls())-2:]))
    click('暂停状态查询')
    sleep(.3)
    paused_count = len(calls())
    sleep(2)
    assert len(calls()) == paused_count
    assert any(b.is_enabled() for b in driver.find_elements(By.TAG_NAME, 'button') if b.text == '读取任务列表')
    click('查看任务状态', article('draft'))
    sleep(2)
    assert all(c['method'] == 'GET' or c['path'].endswith('/resume') for c in calls())
    driver.refresh()
    wait.until(lambda _: len(driver.find_elements(By.CSS_SELECTOR, '.job-history-item')) == 4)
    assert all(c['method'] == 'GET' for c in calls())
    click('查看任务状态', article('draft'))  # Durable result survives reload and still needs explicit apply.
    open_report()
    wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, '.alternative-result'))
    editor = driver.find_element(By.CSS_SELECTOR, 'textarea:not([readonly]).markdown-editor')
    assert editor.get_attribute('value') == 'Manual saved report'
    click('手动应用到编辑区')
    assert editor.get_attribute('value') == 'Durable alternative draft'
    assert not any(c['method'] == 'PATCH' for c in calls())
    driver.find_element(By.CSS_SELECTOR, '.alternative-result').screenshot(str(Path(__file__).with_name('durable-result.png')))
    driver.save_screenshot(str(Path(__file__).with_name('reports-desktop.png')))
    driver.refresh()
    wait.until(lambda _: len(driver.find_elements(By.CSS_SELECTOR, '.job-history-item')) == 4)
    assert all(c['method'] == 'GET' for c in calls())
    driver.execute_script("window.fixture.jobs.splice(0, 1)")
    click('读取任务列表')
    open_report()
    wait.until(lambda _: '创建新任务重试' in driver.find_element(By.TAG_NAME, 'body').text)
    generation = next(p for p in driver.find_elements(By.CSS_SELECTOR, '.panel') if '03 · 一次性生成' in p.text)
    driver.execute_script("arguments[0].value='bound'; arguments[0].dispatchEvent(new Event('change',{bubbles:true}))", generation.find_elements(By.TAG_NAME, 'select')[1])
    for check in generation.find_elements(By.CSS_SELECTOR, 'input[type=checkbox]'):
        driver.execute_script('arguments[0].click()', check)
        sleep(.1)
    click('确认可能重复计费，创建新任务重试')
    retry_body = next(c['body'] for c in calls() if c['path'].endswith('/generate'))
    assert retry_body['retryUnknown'] is True and retry_body['idempotencyKey'] not in ['original-key', 'unknown-key']
    driver.get(base + '?disabled=1')
    wait.until(lambda _: len(driver.find_elements(By.CSS_SELECTOR, '.job-history-item')) == 4)
    open_report()
    wait.until(lambda _: '生成已禁用' in driver.find_element(By.TAG_NAME, 'body').text)
    driver.set_window_size(390, 844)
    generation = next(p for p in driver.find_elements(By.CSS_SELECTOR, '.panel') if '03 · 一次性生成' in p.text)
    generation.screenshot(str(Path(__file__).with_name('reports-mobile-disabled.png')))
    assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
    assert all(c['method'] == 'GET' for c in calls())
    checks = {'checks': 'passed', 'scope': 'fixture API only', 'screenshots': ['reports-desktop.png', 'reports-mobile-disabled.png', 'resume-dialog.png', 'durable-result.png']}
    Path(__file__).with_name('browser-receipt.json').write_text(json.dumps(checks, indent=2) + '\n')
    print(json.dumps(checks))
finally:
    driver.quit()
