#include "led-matrix.h"

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>

#include <X11/Xlib.h>
#include <X11/Xutil.h>

#include <linux/joystick.h>

#include <string>
#include <vector>
#include <fstream>

#define DEFAULT_UPDATE_INTERVAL 10000
#define SCREEENSHOT_X 128
#define SCREEENSHOT_Y 128
#define SCREEENSHOT_WIDTH 128
#define SCREEENSHOT_HEIGHT 128

// ---------------------------------------------------------------------
// Brightness control configuration
// ---------------------------------------------------------------------
// Which joystick device to read. Multiple processes can read the same
// device, so this does not interfere with PICO-8's own gamepad input.
#define JOYSTICK_DEVICE "/dev/input/js0"

// Brightness is persisted here. This lives outside /tmp deliberately -
// /tmp on this system doesn't survive reboots (likely systemd's
// PrivateTmp giving the service its own ephemeral /tmp, or a tmpfs
// mount), so anything meant to persist needs to live elsewhere.
// /var/lib is the conventional place for this kind of small persistent
// application state. The directory needs to be pre-created and made
// writable regardless of which user ends up running this (root at
// first, then whatever the matrix library drops privileges to) - see
// setup instructions.
#define BRIGHTNESS_STATE_FILE "/var/lib/pico8-led/brightness.state"

#define BRIGHTNESS_MIN 5
#define BRIGHTNESS_MAX 100
#define BRIGHTNESS_STEP 5
#define BRIGHTNESS_REPEAT_MS 150  // step repeat rate while combo is held
#define JOYSTICK_RETRY_INTERVAL_SEC 30  // how often to look for a controller

// Button numbers as reported by `jstest /dev/input/js0` (NOT the SDL/
// PICO-8 button names - run jstest and press each button on your pad to
// find the right numbers, then edit these three lines).
// Defaults below match a lot of common SNES-style USB pads:
// 0=B 1=A 2=Y 3=X 4=L 5=R 6=Select 7=Start
#define BTN_MODIFIER 10   // hold this...
#define BTN_DIMMER   6   // ...and press this to dim
#define BTN_BRIGHTER 7   // ...and press this to brighten

// Held together with BTN_MODIFIER, this flips the display between PICO-8
// and the Spotify album-art viewer, including restarting the renderer
// with mode-appropriate PWM settings (see switch_mode.sh for why a
// restart is needed - there's no live API to change those on the fly).
// Different action button, same modifier, so it's a separate combo from
// the brightness one above. Run `jstest /dev/input/js0` to find the
// right number for your pad.
#define BTN_MODE_TOGGLE 11   // e.g. Start

// Runs in the working directory xserver-screen was launched from.
// Backgrounded and logged to a temp file so a slow xdotool call never
// blocks the render loop. Note this script will itself kill the
// currently-running xserver-screen (this process) as part of the mode
// switch - that's expected, and run_led.sh's respawn loop brings it
// back up with the new mode's settings a moment later.
#define TOGGLE_DISPLAY_COMMAND "./switch_mode.sh toggle >/tmp/pico8-led-toggle.log 2>&1 &"

using rgb_matrix::Canvas;
using rgb_matrix::FrameCanvas;
using rgb_matrix::RGBMatrix;

struct ColorComponentModifier
{
    unsigned long shift;
    unsigned long bits;
};

// Make sure we can exit gracefully when Ctrl-C is pressed.
volatile bool interrupt_received = false;
static void InterruptHandler(int signo)
{
    interrupt_received = true;
}

ColorComponentModifier GetColorComponentModifier(unsigned long mask)
{
    ColorComponentModifier color_component_modifier;
    color_component_modifier.shift = 0;
    color_component_modifier.bits = 0;

    while (!(mask & 1))
    {
        color_component_modifier.shift++;
        mask >>= 1;
    }
    while (mask & 1)
    {
        color_component_modifier.bits++;
        mask >>= 1;
    }
    if (color_component_modifier.bits > 8)
    {
        color_component_modifier.shift += color_component_modifier.bits - 8;
        color_component_modifier.bits = 8;
    }
    return color_component_modifier;
}

// ---------------------------------------------------------------------
// Brightness / joystick helpers
// ---------------------------------------------------------------------

static int g_joystick_fd = -1;
static bool g_button_held[512] = {false};
static struct timespec g_last_joystick_attempt = {0, 0};
static bool g_joystick_attempted_once = false;

// Reads the last-saved brightness from disk, or returns fallback if
// there isn't one / it's invalid.
static int LoadSavedBrightness(int fallback)
{
    std::ifstream in(BRIGHTNESS_STATE_FILE);
    int value;
    if (in >> value)
    {
        if (value >= BRIGHTNESS_MIN && value <= BRIGHTNESS_MAX)
            return value;
    }
    return fallback;
}

static void SaveBrightness(int value)
{
    std::ofstream out(BRIGHTNESS_STATE_FILE, std::ios::trunc);
    if (out)
    {
        out << value;
    }
    else
    {
        fprintf(stderr, "Warning: could not write %s (%s). Brightness will not persist across restarts.\n",
                BRIGHTNESS_STATE_FILE, strerror(errno));
    }
}

// Tries to (re)open the joystick device, but only actually attempts once
// every JOYSTICK_RETRY_INTERVAL_SEC seconds (the very first call always
// attempts immediately). Safe to call every frame - it self-throttles.
static void TryOpenJoystick()
{
    if (g_joystick_fd >= 0)
        return;  // already open

    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);

    if (g_joystick_attempted_once)
    {
        long elapsed_ms = (now.tv_sec - g_last_joystick_attempt.tv_sec) * 1000 +
                          (now.tv_nsec - g_last_joystick_attempt.tv_nsec) / 1000000;
        if (elapsed_ms < JOYSTICK_RETRY_INTERVAL_SEC * 1000)
            return;  // not time yet
    }

    g_last_joystick_attempt = now;
    bool first_attempt = !g_joystick_attempted_once;
    g_joystick_attempted_once = true;

    int fd = open(JOYSTICK_DEVICE, O_RDONLY | O_NONBLOCK);
    if (fd >= 0)
    {
        g_joystick_fd = fd;
        memset(g_button_held, 0, sizeof(g_button_held));
        fprintf(stdout, "Joystick connected (%s); brightness hotkeys enabled.\n", JOYSTICK_DEVICE);
    }
    else if (first_attempt)
    {
        fprintf(stdout,
                "Note: %s not available yet (%s). Will keep checking every %d seconds - "
                "brightness hotkeys will start working as soon as your controller is on.\n",
                JOYSTICK_DEVICE, strerror(errno), JOYSTICK_RETRY_INTERVAL_SEC);
    }
    // Silent on failure after the first attempt, to avoid spamming the
    // log every retry interval for as long as the controller is off.
}

static void CloseJoystick(const char *reason)
{
    if (g_joystick_fd >= 0)
    {
        close(g_joystick_fd);
        g_joystick_fd = -1;
        memset(g_button_held, 0, sizeof(g_button_held));
        fprintf(stdout, "Joystick disconnected (%s). Will keep looking for it every %d seconds.\n",
                reason, JOYSTICK_RETRY_INTERVAL_SEC);
    }
}

// Drains any pending joystick events (non-blocking), reconnecting or
// noticing disconnection as needed, and if the brightness combo is
// currently held, steps *brightness up or down.
// Returns true if brightness changed this call.
static bool PollJoystickBrightness(int *brightness)
{
    if (g_joystick_fd < 0)
    {
        TryOpenJoystick();
        return false;
    }

    struct js_event e;
    for (;;)
    {
        ssize_t n = read(g_joystick_fd, &e, sizeof(e));
        if (n == (ssize_t)sizeof(e))
        {
            if (e.type & JS_EVENT_BUTTON)
            {
                if (e.number < 512)
                    g_button_held[e.number] = (e.value != 0);
            }
            continue;  // keep draining pending events
        }

        if (n < 0)
        {
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                break;  // no more events right now - normal case
            // Anything else (commonly ENODEV or EIO when the controller
            // is unplugged or turned off) means the device is gone.
            CloseJoystick(strerror(errno));
            return false;
        }

        if (n == 0)
        {
            CloseJoystick("device closed");  // EOF - device went away
            return false;
        }
    }

    static struct timespec last_step = {0, 0};
    static bool prev_toggle_held = false;
    bool changed = false;

    bool modifier_held = g_button_held[BTN_MODIFIER];
    bool want_brighter = g_button_held[BTN_BRIGHTER];
    bool want_dimmer = g_button_held[BTN_DIMMER];
    bool toggle_held = g_button_held[BTN_MODE_TOGGLE];

    // Mode toggle: fire once per press (edge-triggered), not repeated
    // while held, since this is a discrete on/off switch rather than a
    // value you'd want to keep stepping.
    if (modifier_held && toggle_held && !prev_toggle_held)
    {
        fprintf(stdout, "Toggling display mode (PICO-8 <-> Spotify album art)...\n");
        int rc = system(TOGGLE_DISPLAY_COMMAND);
        (void)rc;  // fire-and-forget; toggle_display.sh logs its own errors
    }
    prev_toggle_held = toggle_held;

    if (modifier_held && (want_brighter || want_dimmer))
    {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        long elapsed_ms = (now.tv_sec - last_step.tv_sec) * 1000 +
                          (now.tv_nsec - last_step.tv_nsec) / 1000000;

        if (elapsed_ms >= BRIGHTNESS_REPEAT_MS)
        {
            if (want_brighter)
                *brightness += BRIGHTNESS_STEP;
            if (want_dimmer)
                *brightness -= BRIGHTNESS_STEP;

            if (*brightness > BRIGHTNESS_MAX)
                *brightness = BRIGHTNESS_MAX;
            if (*brightness < BRIGHTNESS_MIN)
                *brightness = BRIGHTNESS_MIN;

            last_step = now;
            changed = true;
        }
    }

    return changed;
}

// ---------------------------------------------------------------------

int ShowScreen(Display *display, size_t x, size_t y, size_t width, size_t height,
               RGBMatrix *matrix, int update_interval, int initial_brightness)
{
    FrameCanvas *offscreen_canvas = matrix->CreateFrameCanvas();

    int brightness = initial_brightness;
    offscreen_canvas->SetBrightness(brightness);

    XColor color;
    int screen = XDefaultScreen(display);
    XWindowAttributes attribs;
    Window window = XRootWindow(display, screen);
    XImage *img;
    ColorComponentModifier r_modifier, g_modifier, b_modifier;
    unsigned char color_channel[3];

    XGetWindowAttributes(display, window, &attribs);

    // based on original code from http://www.roard.com/docs/cookbook/cbsu19.html
    r_modifier = GetColorComponentModifier(attribs.visual->red_mask);
    g_modifier = GetColorComponentModifier(attribs.visual->green_mask);
    b_modifier = GetColorComponentModifier(attribs.visual->blue_mask);

    while (!interrupt_received)
    {
        if (interrupt_received)
            break;

        if (PollJoystickBrightness(&brightness))
        {
            offscreen_canvas->SetBrightness(brightness);
            SaveBrightness(brightness);
            fprintf(stdout, "Brightness: %d%%\n", brightness);
        }

        img = XGetImage(display, window, x, y, width, height, AllPlanes, XYPixmap);

        for (int xPixel = 0; xPixel < img->width; xPixel++)
        {
            for (int yPixel = 0; yPixel < img->height; yPixel++)
            {
                color.pixel = XGetPixel(img, xPixel, yPixel);
                color_channel[0] = ((color.pixel >> b_modifier.shift) & ((1 << b_modifier.bits) - 1)) << (8 - b_modifier.bits);
                color_channel[1] = ((color.pixel >> g_modifier.shift) & ((1 << g_modifier.bits) - 1)) << (8 - g_modifier.bits);
                color_channel[2] = ((color.pixel >> r_modifier.shift) & ((1 << r_modifier.bits) - 1)) << (8 - r_modifier.bits);
                offscreen_canvas->SetPixel(xPixel, yPixel, color_channel[2], color_channel[1], color_channel[0]);
            }
        }

        offscreen_canvas = matrix->SwapOnVSync(offscreen_canvas);
        // Keep whichever buffer we just got back in sync with the current
        // brightness, in case it was displayed before the last change.
        offscreen_canvas->SetBrightness(brightness);

        XDestroyImage(img);
        usleep(update_interval);
    }
    return 0;
}

int usage(const char *progname)
{
    fprintf(stderr, "Usage: %s [options] [led-matrix-options]\n", progname);
    fprintf(stderr, "Options:\n");
    fprintf(stderr, " -u, --update-interval <microseconds> Update interval in microseconds (default: %d)\n", DEFAULT_UPDATE_INTERVAL);
    fprintf(stderr, " -h, --help Show this help message\n\n");
    fprintf(stderr, "Brightness hotkeys:\n");
    fprintf(stderr, "  Hold button %d + press %d/%d to dim/brighten (see jstest to map to your pad)\n",
            BTN_MODIFIER, BTN_DIMMER, BTN_BRIGHTER);
    fprintf(stderr, "  Last brightness used is saved to '%s' and restored on next start.\n\n",
            BRIGHTNESS_STATE_FILE);
    fprintf(stderr, "LED Matrix options:\n");
    rgb_matrix::PrintMatrixFlags(stderr);
    return 1;
}

int main(int argc, char *argv[])
{
    // When stdout isn't a terminal (e.g. running under systemd, piped to
    // journald), C's stdio defaults to fully-buffered output - meaning
    // our fprintf() calls can sit unflushed for a long time instead of
    // showing up live in `journalctl -f`. Force line-buffering so every
    // log line appears immediately regardless of where stdout is going.
    setvbuf(stdout, NULL, _IOLBF, 0);

    int update_interval = DEFAULT_UPDATE_INTERVAL;

    // Manual parsing of custom options
    std::vector<char *> new_argv;
    new_argv.push_back(argv[0]);
    for (int i = 1; i < argc; ++i)
    {
        const std::string arg = argv[i];
        if (arg == "-u" || arg == "--update-interval")
        {
            if (i + 1 < argc)
            {
                update_interval = atoi(argv[++i]);
                if (update_interval <= 0)
                {
                    fprintf(stderr, "Error: Update interval must be positive\n");
                    return 1;
                }
            }
            else
            {
                fprintf(stderr, "Error: %s requires an argument\n", arg.c_str());
                return usage(argv[0]);
            }
        }
        else if (arg == "-h" || arg == "--help")
        {
            return usage(argv[0]);
        }
        else
        {
            new_argv.push_back(argv[i]);
        }
    }

    int new_argc = new_argv.size();
    char **new_argv_ptr = new_argv.data();

    // Initialize the RGB matrix
    RGBMatrix::Options matrix_options;
    rgb_matrix::RuntimeOptions runtime_opt;
    if (!rgb_matrix::ParseOptionsFromFlags(&new_argc, &new_argv_ptr,
                                            &matrix_options, &runtime_opt))
    {
        return usage(argv[0]);
    }

    signal(SIGTERM, InterruptHandler);
    signal(SIGINT, InterruptHandler);

    // Try to open the joystick device *before* the matrix library drops
    // root privileges (it does this internally right after GPIO init).
    // Once opened, the file descriptor stays valid even after the
    // privilege drop. If no controller is connected yet, this is safe -
    // PollJoystickBrightness() will keep retrying periodically at runtime.
    TryOpenJoystick();

    RGBMatrix *matrix = RGBMatrix::CreateFromOptions(matrix_options, runtime_opt);
    if (matrix == NULL)
        return 1;

    Display *display;
    char *dpy_name = std::getenv("DISPLAY");
    if (!dpy_name)
    {
        fprintf(stderr, "No DISPLAY set\n");
        return 1;
    }
    fprintf(stdout, "DISPLAY is %s:\n", dpy_name);
    fprintf(stdout, "Update interval: %d microseconds (%.1f FPS)\n",
            update_interval, 1000000.0 / update_interval);

    display = XOpenDisplay(dpy_name);
    if (display == NULL)
    {
        fprintf(stderr, "Display %s cannot be found, exiting", dpy_name);
        return 1;
    }

    // --led-brightness is used as the fallback default; a previously
    // saved brightness (from the hotkeys) takes priority if present.
    int initial_brightness = LoadSavedBrightness(matrix_options.brightness);
    fprintf(stdout, "Starting brightness: %d%%\n", initial_brightness);

    // Put screnshot from display on RGB Matrix
    ShowScreen(display, SCREEENSHOT_X, SCREEENSHOT_Y, SCREEENSHOT_WIDTH, SCREEENSHOT_HEIGHT,
               matrix, update_interval, initial_brightness);

    if (g_joystick_fd >= 0)
        close(g_joystick_fd);

    XCloseDisplay(display);
    matrix->Clear();
    delete matrix;
    return 0;
}
