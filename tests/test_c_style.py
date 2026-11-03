import unittest

from review_agent.context import _c_style_definition


class CStyleDefinitionTest(unittest.TestCase):
    def test_recognizes_definitions_and_declarations(self):
        for line, name in [
            ("double average(const std::vector<int>& values) {", "average"),
            ("int* copy_values(const std::vector<int>& values) {", "copy_values"),
            ("void print_values(const int* values, int count)", "print_values"),
            ("static std::map<std::string, int> load_table(const char *path) {", "load_table"),
            ("public static int Parse(string text) {", "Parse"),
            ("int* copy_values(const std::vector<int>& values)", "copy_values"),  # declaration minus `;`
        ]:
            self.assertEqual(_c_style_definition(line), name, line)

    def test_ignores_calls_statements_and_control_flow(self):
        for line in [
            "    return copy_values(scores);",
            "    return copy_values(scores)",
            "    int* copy = copy_values(scores);",
            "    print_values(copy, scores.size());",
            "    std::memcpy(buffer, values.data(), values.size());",
            "    for (size_t i = 0; i <= values.size(); ++i) {",
            "    } else if (ready) {",
            "    while (running) {",
        ]:
            self.assertIsNone(_c_style_definition(line), line)


if __name__ == "__main__":
    unittest.main()
