#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <U8g2lib.h>
#include "driver/i2s.h"
#include <math.h>

// ================================================
// NETWORK — change SERVER_IP to your cloud URL later
// ================================================
// !! UPDATE THESE WITH YOUR WIFI CREDENTIALS !!
const char* ssid     = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

#define SERVER_IP   "192.168.1.5"
#define SERVER_PORT "5000"

const char* audioUrl    = "http://" SERVER_IP ":" SERVER_PORT "/audio";
const char* triggerUrl  = "http://" SERVER_IP ":" SERVER_PORT "/trigger";
const char* getAudioUrl = "http://" SERVER_IP ":" SERVER_PORT "/get_audio";
const char* getEyeUrl   = "http://" SERVER_IP ":" SERVER_PORT "/get_eye";

// ================================================
// OLED
// ================================================
#define OLED_SDA 21
#define OLED_SCL 19
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, OLED_SCL, OLED_SDA);

volatile int eye_state = 0;

// ================================================
// HARDWARE
// ================================================
#define TOUCH_PIN        T0
#define TOUCH_THRESHOLD  40
#define TOUCH_DEBOUNCE   3000
#define MIC_WS   25
#define MIC_SD   33
#define MIC_SCK  26
#define SAMPLE_BUFFER_SIZE 1024
#define SOFTWARE_GAIN      3
#define BATCH_SIZE         8
int32_t i2sBuffer[SAMPLE_BUFFER_SIZE * 2];
int16_t sendBuffer[SAMPLE_BUFFER_SIZE * BATCH_SIZE];
int     sendBufferPos = 0;
int     chunkCount    = 0;
#define SPK_BCLK  27
#define SPK_LRC   14
#define SPK_DIN   22
#define SPK_RATE  16000
unsigned long lastTriggerMs = 0;
bool lastTouched = false;

// Poll timing
unsigned long lastAudioPoll = 0;
unsigned long lastEyePoll   = 0;
#define AUDIO_POLL_MS  300
#define EYE_POLL_MS    500

// ================================================
// JARVIS ARC REACTOR HUD
// ================================================
#define CX 64
#define CY 32

float angle1 = 0, angle2 = 0, angle3 = 0;
float pulsePhase = 0;
float coreGlow = 1.0f;
float ringSpeed1 = 0.02f, ringSpeed2 = 0.015f, ringSpeed3 = 0.025f;
float waveRadius = 0;
int frameTick = 0;
bool bootDone = false;
int  bootFrame = 0;

void drawArc(int cx, int cy, int r, float startA, float endA) {
    float step = 1.5f / (float)r;
    for (float a = startA; a < endA; a += step) {
        int x = cx + (int)(r * cosf(a));
        int y = cy + (int)(r * sinf(a));
        if (x >= 0 && x < 127 && y >= 0 && y < 64)
            u8g2.drawPixel(x, y);
    }
}

void drawArcThick(int cx, int cy, int r, float startA, float endA) {
    drawArc(cx, cy, r, startA, endA);
    drawArc(cx, cy, r - 1, startA, endA);
}

void drawTicks(int cx, int cy, int r, int count, int len, float offset) {
    for (int i = 0; i < count; i++) {
        float a = offset + (2.0f * M_PI * i) / count;
        int x1 = cx + (int)(r * cosf(a));
        int y1 = cy + (int)(r * sinf(a));
        int x2 = cx + (int)((r - len) * cosf(a));
        int y2 = cy + (int)((r - len) * sinf(a));
        if (x1 >= 0 && x1 < 127 && x2 >= 0 && x2 < 127)
            u8g2.drawLine(x1, y1, x2, y2);
    }
}

void drawJarvisHUD() {
    int es = eye_state;
    float dt;

    if (es == 0) {
        dt = 1.0f;
        ringSpeed1 = 0.012f; ringSpeed2 = 0.008f; ringSpeed3 = 0.018f;
        coreGlow += (0.6f + 0.3f * sinf(frameTick * 0.04f) - coreGlow) * 0.1f;
    } else if (es == 1) {
        dt = 2.5f;
        ringSpeed1 = 0.04f; ringSpeed2 = 0.03f; ringSpeed3 = 0.05f;
        coreGlow += (1.0f - coreGlow) * 0.15f;
    } else if (es == 2) {
        dt = 3.0f;
        ringSpeed1 = 0.06f; ringSpeed2 = 0.045f; ringSpeed3 = 0.07f;
        coreGlow += (0.4f + 0.6f * fabsf(sinf(frameTick * 0.1f)) - coreGlow) * 0.2f;
    } else if (es == 3) {
        dt = 1.8f;
        ringSpeed1 = 0.025f; ringSpeed2 = 0.02f; ringSpeed3 = 0.035f;
        coreGlow += (0.8f + 0.2f * sinf(frameTick * 0.15f) - coreGlow) * 0.15f;
    } else {
        dt = 1.0f;
    }

    angle1 += ringSpeed1 * dt;
    angle2 -= ringSpeed2 * dt;
    angle3 += ringSpeed3 * dt;
    pulsePhase += 0.05f * dt;
    if (angle1 > 2*M_PI) angle1 -= 2*M_PI;
    if (angle2 < -2*M_PI) angle2 += 2*M_PI;
    if (angle3 > 2*M_PI) angle3 -= 2*M_PI;

    u8g2.clearBuffer();

    // Outer ring
    drawArcThick(CX,CY,30, angle1, angle1+1.2f);
    drawArcThick(CX,CY,30, angle1+2.1f, angle1+3.0f);
    drawArcThick(CX,CY,30, angle1+4.2f, angle1+5.4f);
    drawTicks(CX,CY,30,24,2,angle1*0.5f);

    // Middle ring
    drawArc(CX,CY,22, angle2, angle2+0.8f);
    drawArc(CX,CY,22, angle2+1.57f, angle2+2.4f);
    drawArc(CX,CY,22, angle2+3.14f, angle2+3.9f);
    drawArc(CX,CY,22, angle2+4.71f, angle2+5.5f);

    // Inner ring
    drawArcThick(CX,CY,14, angle3, angle3+2.0f);
    drawArcThick(CX,CY,14, angle3+3.14f, angle3+5.14f);
    drawTicks(CX,CY,14,12,2,-angle3);

    // Core
    int coreR = 3 + (int)(2.0f * coreGlow * fabsf(sinf(pulsePhase)));
    u8g2.drawDisc(CX,CY,coreR);
    u8g2.setDrawColor(0);
    if (coreR>2) u8g2.drawDisc(CX,CY,coreR-2);
    u8g2.setDrawColor(1);
    u8g2.drawDisc(CX,CY,1);

    // Sound waves (speaking)
    if (es == 3) {
        waveRadius += 0.8f;
        if (waveRadius > 35) waveRadius = 8;
        int wr = (int)waveRadius;
        for (float a = 0; a < 2*M_PI; a += 0.08f) {
            int x = CX + (int)(wr * cosf(a));
            int y = CY + (int)(wr * sinf(a));
            if (x >= 0 && x < 127 && y >= 0 && y < 64)
                if (((int)(a*10)+frameTick)%3!=0) u8g2.drawPixel(x,y);
        }
    }

    // Scan line (thinking)
    if (es == 2) {
        float sa = frameTick * 0.12f;
        u8g2.drawLine(CX,CY, CX+(int)(32*cosf(sa)), CY+(int)(32*sinf(sa)));
    }

    // HUD brackets
    u8g2.drawHLine(0,0,10); u8g2.drawVLine(0,0,5);
    u8g2.drawHLine(118,0,9); u8g2.drawVLine(126,0,5);
    u8g2.drawHLine(0,63,10); u8g2.drawVLine(0,59,5);
    u8g2.drawHLine(118,63,9); u8g2.drawVLine(126,59,5);

    // Status text
    u8g2.setFont(u8g2_font_4x6_tr);
    u8g2.drawStr(2,9,"JARVIS");
    if (es==0) { u8g2.drawStr(97,9,"READY"); u8g2.drawStr(2,62,"IDLE"); }
    else if (es==1) { u8g2.drawStr(97,9,"ACTV"); u8g2.drawStr(2,62,"REC");
        if (frameTick%10<5) u8g2.drawDisc(16,59,1); }
    else if (es==2) { u8g2.drawStr(97,9,"PROC"); u8g2.drawStr(2,62,"SCAN"); }
    else if (es==3) { u8g2.drawStr(97,9,"COMMS"); u8g2.drawStr(2,62,"TX");
        for (int i=0;i<4;i++) { int bh=2+(int)(4.0f*fabsf(sinf(frameTick*0.2f+i*0.5f)));
            u8g2.drawBox(108+i*3,62-bh,2,bh); } }

    u8g2.sendBuffer();
}

void drawBoot() {
    u8g2.clearBuffer();
    if (bootFrame<20) {
        float dp=(float)bootFrame/20.0f;
        int r=1+(int)(3.0f*dp);
        u8g2.drawDisc(CX,CY,r);
    } else if (bootFrame<40) {
        float rp=(float)(bootFrame-20)/20.0f;
        u8g2.drawDisc(CX,CY,3);
        u8g2.setDrawColor(0); u8g2.drawDisc(CX,CY,1); u8g2.setDrawColor(1);
        u8g2.drawPixel(CX,CY);
        drawArc(CX,CY,14,0,rp*2*M_PI);
    } else if (bootFrame<60) {
        float rp=(float)(bootFrame-40)/20.0f;
        u8g2.drawDisc(CX,CY,3);
        u8g2.setDrawColor(0); u8g2.drawDisc(CX,CY,1); u8g2.setDrawColor(1);
        u8g2.drawPixel(CX,CY);
        drawArc(CX,CY,14,0,2*M_PI);
        drawArc(CX,CY,22,0,rp*2*M_PI);
        if (rp>0.3f) drawArc(CX,CY,30,0,rp*0.7f*2*M_PI);
    } else {
        float tp=(float)(bootFrame-60)/20.0f;
        if (tp>1.0f) tp=1.0f;
        u8g2.drawDisc(CX,CY,3);
        u8g2.setDrawColor(0); u8g2.drawDisc(CX,CY,1); u8g2.setDrawColor(1);
        u8g2.drawPixel(CX,CY);
        drawArc(CX,CY,14,0,2*M_PI);
        drawArc(CX,CY,22,0,2*M_PI);
        drawArcThick(CX,CY,30,0,2*M_PI);
        drawTicks(CX,CY,30,24,2,0);
        if (tp>0.3f) { u8g2.setFont(u8g2_font_4x6_tr); u8g2.drawStr(2,9,"JARVIS"); u8g2.drawStr(97,9,"v2.0"); }
        if (tp>0.6f) u8g2.drawStr(32,62,"SYSTEM ONLINE");
        if (tp>0.4f) {
            u8g2.drawHLine(0,0,10); u8g2.drawVLine(0,0,5);
            u8g2.drawHLine(118,0,9); u8g2.drawVLine(126,0,5);
            u8g2.drawHLine(0,63,10); u8g2.drawVLine(0,59,5);
            u8g2.drawHLine(118,63,9); u8g2.drawVLine(126,59,5);
        }
    }
    u8g2.sendBuffer();
    bootFrame++;
    if (bootFrame>=80) bootDone=true;
}

// OLED task on Core 0
void oledTask(void* param) {
    while (true) {
        frameTick++;
        if (!bootDone) drawBoot(); else drawJarvisHUD();
        vTaskDelay(50 / portTICK_PERIOD_MS);
    }
}

// Poll for audio from server and play it
void pollAudio() {
    if (WiFi.status() != WL_CONNECTED) return;
    HTTPClient http;
    http.begin(getAudioUrl);
    int code = http.GET();
    if (code == 200) {
        int len = http.getSize();
        if (len > 0) {
            Serial.printf("[SPK] Got %d bytes PCM\n", len);
            eye_state = 3;
            WiFiClient* stream = http.getStreamPtr();
            uint8_t buf[512];
            i2s_zero_dma_buffer(I2S_NUM_1);
            while (http.connected() && len > 0) {
                int toRead = min((int)sizeof(buf), len);
                int got = stream->readBytes(buf, toRead);
                if (got > 0) {
                    size_t written;
                    i2s_write(I2S_NUM_1, buf, got, &written, portMAX_DELAY);
                    len -= got;
                }
            }
            i2s_zero_dma_buffer(I2S_NUM_1);
            eye_state = 0;
            Serial.println("[SPK] Done");
        }
    }
    http.end();
}

// Poll for eye state from server
void pollEye() {
    if (WiFi.status() != WL_CONNECTED) return;
    HTTPClient http;
    http.begin(getEyeUrl);
    int code = http.GET();
    if (code == 200) {
        String s = http.getString();
        eye_state = s.toInt();
    }
    http.end();
}

// ================================================
// I2S
// ================================================
void setupI2SMic() {
    i2s_config_t cfg = {
        .mode               = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate        = 16000,
        .bits_per_sample    = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format     = I2S_CHANNEL_FMT_RIGHT_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags   = 0,
        .dma_buf_count      = 8,
        .dma_buf_len        = 64,
        .use_apll           = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk         = 0
    };
    i2s_pin_config_t pins = {
        .bck_io_num   = MIC_SCK,
        .ws_io_num    = MIC_WS,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num  = MIC_SD
    };
    i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
    i2s_set_pin(I2S_NUM_0, &pins);
    size_t dummy;
    for (int i = 0; i < 10; i++)
        i2s_read(I2S_NUM_0, i2sBuffer, sizeof(i2sBuffer), &dummy, portMAX_DELAY);
}

void setupI2SSpeaker() {
    i2s_config_t cfg = {
        .mode               = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
        .sample_rate        = SPK_RATE,
        .bits_per_sample    = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format     = I2S_CHANNEL_FMT_RIGHT_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags   = 0,
        .dma_buf_count      = 8,
        .dma_buf_len        = 512,
        .use_apll           = false,
        .tx_desc_auto_clear = true,
    };
    i2s_pin_config_t pins = {
        .bck_io_num   = SPK_BCLK,
        .ws_io_num    = SPK_LRC,
        .data_out_num = SPK_DIN,
        .data_in_num  = I2S_PIN_NO_CHANGE,
    };
    i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
    i2s_set_pin(I2S_NUM_1, &pins);
    i2s_zero_dma_buffer(I2S_NUM_1);
}

void connectWiFi() {
    WiFi.begin(ssid, password);
    while (WiFi.status() != WL_CONNECTED) delay(500);
    Serial.println("[OK] WiFi: " + WiFi.localIP().toString());
}

void sendTrigger() {
    HTTPClient http;
    http.begin(triggerUrl);
    http.addHeader("Content-Type", "application/json");
    http.POST("{}");
    http.end();
}

// ================================================
void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\n=== JARVIS ===");
    Wire.begin(OLED_SDA, OLED_SCL);
    u8g2.begin();
    u8g2.clearBuffer();
    u8g2.sendBuffer();
    connectWiFi();
    setupI2SMic();
    setupI2SSpeaker();
    xTaskCreatePinnedToCore(oledTask, "OLED", 8192, NULL, 1, NULL, 0);
    Serial.println("===============\n");
}

void loop() {
    unsigned long now = millis();

    // Poll for TTS audio
    if (now - lastAudioPoll >= AUDIO_POLL_MS) {
        lastAudioPoll = now;
        pollAudio();
    }

    // Poll for eye state
    if (now - lastEyePoll >= EYE_POLL_MS) {
        lastEyePoll = now;
        pollEye();
    }

    // Touch
    uint16_t tv = touchRead(TOUCH_PIN);
    bool touched = (tv < TOUCH_THRESHOLD);
    if (touched && !lastTouched && (now - lastTriggerMs > TOUCH_DEBOUNCE)) {
        sendTrigger();
        lastTriggerMs = now;
    }
    lastTouched = touched;

    // Mic
    size_t bytesRead;
    i2s_read(I2S_NUM_0, i2sBuffer, sizeof(i2sBuffer), &bytesRead, portMAX_DELAY);
    int frames = (bytesRead / 4) / 2;
    for (int i = 0; i < frames; i++) {
        int32_t s = (i2sBuffer[i * 2 + 1] >> 16) * SOFTWARE_GAIN;
        if (s >  32767) s =  32767;
        if (s < -32768) s = -32768;
        if (sendBufferPos < SAMPLE_BUFFER_SIZE * BATCH_SIZE)
            sendBuffer[sendBufferPos++] = (int16_t)s;
    }
    chunkCount++;

    // Send audio batch
    if (sendBufferPos >= SAMPLE_BUFFER_SIZE * BATCH_SIZE) {
        if (WiFi.status() == WL_CONNECTED) {
            HTTPClient http;
            http.begin(audioUrl);
            http.addHeader("Content-Type", "application/octet-stream");
            http.POST((uint8_t*)sendBuffer, sendBufferPos * sizeof(int16_t));
            http.end();
        }
        sendBufferPos = 0;
    }
}
