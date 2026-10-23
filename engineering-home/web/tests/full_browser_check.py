"""Production UI + real auth/API exercised with fixture-only integrations."""
import os
import shutil
from pathlib import Path
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
driver = webdriver.Firefox(options=options, service=Service(os.environ.get('EH_GECKODRIVER') or shutil.which('geckodriver'), log_output=str(Path(__file__).with_name('geckodriver.log'))))
wait = WebDriverWait(driver, 15)
output = Path(__file__).parent

def click(text, scope=None):
    button = wait.until(lambda _: next((b for b in (scope or driver).find_elements(By.TAG_NAME, 'button') if b.text == text and b.is_enabled()), None))
    button.click()

def fill(label, value, scope=None):
    container = next(item for item in (scope or driver).find_elements(By.TAG_NAME, 'label') if label in item.text)
    field = container.find_elements(By.CSS_SELECTOR, 'input,textarea')[0]
    field.clear()
    field.send_keys(value)

def nav(page):
    driver.get('http://127.0.0.1:4319/#' + page)
    wait.until(lambda _: driver.find_elements(By.TAG_NAME, 'h1'))

try:
    driver.set_window_size(1440, 1000)
    driver.get('http://127.0.0.1:4319/')
    wait.until(lambda _: '回到你的工程桌面' in driver.find_element(By.TAG_NAME, 'body').text)
    fill('访问密码', 'dummy-test-password-only')
    click('安全登录')
    wait.until(lambda _: '让工程日常' in driver.find_element(By.TAG_NAME, 'h1').text)
    driver.save_screenshot(str(output / 'overview-desktop.png'))
    nav('mrs')
    click('从 GitLab 刷新')
    wait.until(lambda _: 'Fixture: 工程快捷台 MR' in driver.find_element(By.TAG_NAME, 'body').text)
    driver.save_screenshot(str(output / 'mrs-desktop.png'))
    nav('accounts')
    click('刷新全部余额')
    wait.until(lambda _: '推广预算' in driver.find_element(By.TAG_NAME, 'body').text)
    assert '独立共享池' in driver.find_element(By.TAG_NAME, 'body').text
    click('编辑标记')
    fill('显示别名', '审核专用')
    fill('用途标签', 'code-review, weekly-report')
    click('保存标记')
    wait.until(lambda _: driver.find_elements(By.XPATH, '//h2[text()="审核专用"]'))
    driver.refresh()
    wait.until(lambda _: 'code-review' in driver.find_element(By.TAG_NAME, 'body').text)
    driver.save_screenshot(str(output / 'accounts-desktop.png'))
    nav('services')
    click('重新发现服务')
    wait.until(lambda _: 'Fixture web' in driver.find_element(By.TAG_NAME, 'body').text)
    assert '后台服务，无网络端点' in driver.find_element(By.TAG_NAME, 'body').text
    card = next(panel for panel in driver.find_elements(By.CSS_SELECTOR, '.panel') if 'Fixture web' in panel.text)
    click('编辑说明与入口', card)
    select = card.find_element(By.TAG_NAME, 'select')
    driver.execute_script("arguments[0].value='http'; arguments[0].dispatchEvent(new Event('change',{bubbles:true}))", select)
    fill('说明', '测试网页，远程可达性未验证', card)
    click('保存说明', card)
    wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, 'a[href="http://192.0.2.10:8090/"]'))
    driver.save_screenshot(str(output / 'services-desktop.png'))
    nav('reports')
    for check in driver.find_elements(By.CSS_SELECTOR, 'input[type=checkbox]')[:2]: check.click()
    click('采集证据')
    wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, 'textarea.markdown-editor:not([readonly])'))
    editor = driver.find_element(By.CSS_SELECTOR, 'textarea.markdown-editor:not([readonly])')
    editor.send_keys('Situation\n- 测试\nAction\n- 测试\nOthers:\n- 无\nResult\n- 待测\nTodo\n- 验证')
    click('保存正文')
    wait.until(lambda _: '正文已保存' in driver.find_element(By.TAG_NAME, 'body').text)
    driver.execute_script("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>window.copiedText=t}})")
    click('复制周报')
    wait.until(lambda _: '已复制纯文本' in driver.find_element(By.TAG_NAME, 'body').text)
    assert 'Situation' in driver.execute_script('return window.copiedText')
    driver.execute_script("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('denied')}}})")
    click('复制周报')
    wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, 'textarea[aria-label="手动复制纯文本"]'))
    driver.save_screenshot(str(output / 'reports-full-desktop.png'))
    nav('overview')
    wait.until(lambda _: '审核专用' in driver.find_element(By.TAG_NAME, 'body').text)
    driver.save_screenshot(str(output / 'overview-populated.png'))
    driver.set_window_size(390, 844)
    wait.until(lambda _: driver.find_elements(By.CSS_SELECTOR, '.mobile-toggle'))
    driver.save_screenshot(str(output / 'overview-mobile.png'))
    assert driver.execute_script('return document.documentElement.scrollWidth <= innerWidth')
    assert driver.execute_script('return localStorage.length') == 0
    assert driver.execute_script('return sessionStorage.length') == 0
    print('Full UI fixture: login/MR/budgets/tags/services/report-save/copy+fallback/mobile passed; no live providers or model calls')
finally:
    driver.quit()
