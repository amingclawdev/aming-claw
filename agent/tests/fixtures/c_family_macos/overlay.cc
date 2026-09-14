#include "overlay.h"
#if 0
#include "inactive_only.h"
#endif

namespace overlay {
int render(int value) { return value + selected_feature(); }
int render(TextView value) { return value.size; }
int selected_feature() { return 1; }
int Base::value() const { return 0; }
int Derived::value() const { return render(2); }
}
