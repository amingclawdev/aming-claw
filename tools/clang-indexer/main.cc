#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#include <string>
#include <vector>

static const char* kSchema = "aming_claw.cfamily_clang_index.v1";

static int usage(const char* message) {
  fprintf(stderr, "%s\nusage: aming-claw-clang-indexer --clang PATH -- ARGS...\n", message);
  return 64;
}

static std::string read_stream(FILE* stream) {
  std::string output;
  char buffer[16384];
  rewind(stream);
  while (true) {
    const size_t count = fread(buffer, 1, sizeof(buffer), stream);
    output.append(buffer, count);
    if (count < sizeof(buffer)) break;
  }
  return output;
}

static std::string json_string(const std::string& value) {
  static const char hex[] = "0123456789abcdef";
  std::string output = "\"";
  for (const unsigned char ch : value) {
    switch (ch) {
      case '\\': output += "\\\\"; break;
      case '"': output += "\\\""; break;
      case '\n': output += "\\n"; break;
      case '\r': output += "\\r"; break;
      case '\t': output += "\\t"; break;
      default:
        if (ch < 0x20) {
          output += "\\u00";
          output += hex[(ch >> 4) & 0xf];
          output += hex[ch & 0xf];
        } else {
          output += static_cast<char>(ch);
        }
    }
  }
  output += "\"";
  return output;
}

int main(int argc, char** argv) {
  if (argc == 2 && strcmp(argv[1], "--version") == 0) {
    printf("{\"schema_version\":\"%s\",\"extractor_version\":\"1\"}\n", kSchema);
    return 0;
  }
  if (argc < 5 || strcmp(argv[1], "--clang") != 0) return usage("missing --clang");
  if (strcmp(argv[3], "--") != 0) return usage("missing argument separator");

  FILE* clang_stdout = tmpfile();
  FILE* clang_stderr = tmpfile();
  if (!clang_stdout || !clang_stderr) {
    fprintf(stderr, "temporary capture unavailable: %s\n", strerror(errno));
    return 74;
  }
  std::vector<char*> child_argv;
  child_argv.push_back(argv[2]);
  for (int index = 4; index < argc; ++index) child_argv.push_back(argv[index]);
  child_argv.push_back(nullptr);

  const pid_t pid = fork();
  if (pid < 0) {
    fprintf(stderr, "clang fork failed: %s\n", strerror(errno));
    return 71;
  }
  if (pid == 0) {
    if (dup2(fileno(clang_stdout), STDOUT_FILENO) < 0 || dup2(fileno(clang_stderr), STDERR_FILENO) < 0) _exit(126);
    execv(argv[2], child_argv.data());
    fprintf(stderr, "clang exec failed: %s\n", strerror(errno));
    _exit(errno == ENOENT ? 127 : 126);
  }

  int status = 0;
  while (waitpid(pid, &status, 0) < 0) {
    if (errno != EINTR) return 71;
  }
  const int returncode = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + (WIFSIGNALED(status) ? WTERMSIG(status) : 0);
  const std::string ast = read_stream(clang_stdout);
  const std::string diagnostics = read_stream(clang_stderr);
  fclose(clang_stdout);
  fclose(clang_stderr);

  const bool ast_object = !ast.empty() && ast.front() == '{';
  printf("{\"schema_version\":\"%s\",\"extractor_version\":\"1\",\"status\":\"%s\",\"returncode\":%d,\"ast_json\":%s,\"stderr\":%s}\n",
         kSchema, returncode == 0 && ast_object ? "ok" : "failed", returncode,
         ast_object ? ast.c_str() : "null", json_string(diagnostics).c_str());
  return 0;
}
