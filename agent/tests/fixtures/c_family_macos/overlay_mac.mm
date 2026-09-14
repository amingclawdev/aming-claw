#import <AppKit/AppKit.h>
#include "overlay.h"

int render_window_title(NSWindow *window) {
  return overlay::render([[window title] length]);
}
