#pragma once

#include <c10/util/Backtrace.h>
#include <c10/util/Optional.h>
#include <torch/csrc/jit/frontend/tree.h>

namespace torch {
namespace jit {

struct Call {
  std::string fn_name;
  SourceRange caller_range;
};

struct TORCH_API ErrorReport : public std::exception {
  ErrorReport(const ErrorReport& e);

  explicit ErrorReport(
      SourceRange r,
      const c10::optional<std::string>& backtrace = c10::nullopt);
  explicit ErrorReport(
      const TreeRef& tree,
      const c10::optional<std::string>& backtrace = c10::nullopt)
      : ErrorReport(tree->range(), backtrace) {}
  explicit ErrorReport(
      const Token& tok,
      const c10::optional<std::string>& backtrace = c10::nullopt)
      : ErrorReport(tok.range, backtrace) {}

  const char* what() const noexcept override;

  struct TORCH_API CallStack {
    // These functions are used to report why a function was being compiled
    // (i.e. what was the call stack of user functions at compilation time that
    // led to this error)
    CallStack(const std::string& name, const SourceRange& range);
    ~CallStack();

    // Change the range that is relevant for the current function (i.e. after
    // each successful expression compilation, change it to the next expression)
    static void update_pending_range(const SourceRange& range);
  };

  static std::string current_call_stack();

 private:
  template <typename T>
  friend const ErrorReport& operator<<(const ErrorReport& e, const T& t);

  mutable std::stringstream ss;
  OwnedSourceRange context;
  mutable std::string the_message;
  std::vector<Call> error_stack;
  std::string backtrace_;
};

template <typename T>
const ErrorReport& operator<<(const ErrorReport& e, const T& t) {
  e.ss << t;
  return e;
}

} // namespace jit
} // namespace torch
