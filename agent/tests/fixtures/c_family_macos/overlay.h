#pragma once
namespace overlay {
struct TextView { int size; };
int render(int value);
int render(TextView value);
#if OVERLAY_FEATURE
int selected_feature();
#else
int unselected_feature();
#endif
struct Base { virtual int value() const; };
struct Derived : Base { int value() const override; };
}
