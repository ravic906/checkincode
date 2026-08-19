"""
Problem bank for the SQL practice MVP.

Each problem is self-contained: its own schema, its own seed data, and a
canonical query used both to (a) compute the expected output at problem-load
time and (b) as a reference for the AI explanation prompt.

`order_matters=False` means the grader compares results as multisets (sorted
before comparing) rather than caring about row order -- most problems here
don't ask for a specific order, so getting ORDER BY "wrong" shouldn't fail you.
Problems that explicitly ask for an order (e.g. "top N by X") set it True.
"""

PROBLEMS = [
    {
        "id": "easy-1-filter-active-employees",
        "title": "Active Employees in Engineering",
        "difficulty": "easy",
        "tags": ["select", "where"],
        "description": (
            "The `employees` table has some rows with a NULL `department` "
            "(not yet assigned) and a couple of duplicate names (rehires). "
            "Return `employee_id`, `full_name`, and `salary` for employees "
            "who are in the 'Engineering' department AND currently active "
            "(`is_active = TRUE`). Do not include NULL departments."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                department VARCHAR,
                salary INTEGER,
                is_active BOOLEAN
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 'Engineering', 95000, TRUE),
            (2, 'Vikram Shah', 'Engineering', 88000, FALSE),
            (3, 'Priya Nair', 'Sales', 62000, TRUE),
            (4, 'Rahul Verma', NULL, 55000, TRUE),
            (5, 'Asha Rao', 'Engineering', 97000, TRUE),
            (6, 'Divya Iyer', 'Engineering', 91000, TRUE),
            (7, 'Karan Mehta', 'Marketing', 60000, TRUE),
            (8, 'Neha Joshi', 'Engineering', NULL, TRUE);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, salary
            FROM employees
            WHERE department = 'Engineering' AND is_active = TRUE;
        """,
    },
    {
        "id": "easy-2-distinct-customers",
        "title": "Which Customers Have Ordered?",
        "difficulty": "easy",
        "tags": ["select", "distinct"],
        "description": (
            "The `orders` table contains one row per order; a customer can "
            "have many orders (or none, tracked in `customers`). Some "
            "`order_amount` values are NULL (order placed but payment not "
            "captured yet). Return the distinct `customer_id`s that appear "
            "in `orders` at all, regardless of amount, sorted ascending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (101, 1, 250.00),
            (102, 2, NULL),
            (103, 1, 90.50),
            (104, 3, 400.00),
            (105, 2, 75.00),
            (106, 4, NULL);
        """,
        "canonical_sql": """
            SELECT DISTINCT customer_id
            FROM orders
            ORDER BY customer_id;
        """,
    },
    {
        "id": "medium-1-customers-without-orders",
        "title": "Customers Who Never Ordered",
        "difficulty": "medium",
        "tags": ["joins", "left-join", "null-handling"],
        "description": (
            "Using `customers` and `orders`, return the `customer_id` and "
            "`customer_name` of customers who have never placed an order. "
            "Sort by `customer_id` ascending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE customers (
                customer_id INTEGER,
                customer_name VARCHAR
            );
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO customers VALUES
            (1, 'Ananya Traders'),
            (2, 'Bharat Textiles'),
            (3, 'Chennai Foods'),
            (4, 'Delta Logistics'),
            (5, 'Everest Retail');

            INSERT INTO orders VALUES
            (101, 1, 250.00),
            (102, 3, 400.00),
            (103, 1, 90.50),
            (104, 5, 120.00);
        """,
        "canonical_sql": """
            SELECT c.customer_id, c.customer_name
            FROM customers c
            LEFT JOIN orders o ON c.customer_id = o.customer_id
            WHERE o.order_id IS NULL
            ORDER BY c.customer_id;
        """,
    },
    {
        "id": "medium-2-employee-department-join",
        "title": "Employees With Department Names",
        "difficulty": "medium",
        "tags": ["joins", "inner-join"],
        "description": (
            "The `departments` table accidentally has a duplicate row for "
            "'Engineering' (dept_id 10 and 40, both named 'Engineering' -- "
            "a data entry mistake that happens in real warehouses). Join "
            "`employees` to `departments` on `dept_id` and return "
            "`employee_id`, `full_name`, and `department_name` for every "
            "employee who has a matching department. Watch out for the "
            "duplicate causing extra rows."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE departments (
                dept_id INTEGER,
                department_name VARCHAR
            );
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                dept_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO departments VALUES
            (10, 'Engineering'),
            (20, 'Sales'),
            (30, 'Marketing'),
            (40, 'Engineering');

            INSERT INTO employees VALUES
            (1, 'Asha Rao', 10),
            (2, 'Priya Nair', 20),
            (3, 'Karan Mehta', 30),
            (4, 'Rahul Verma', NULL);
        """,
        "canonical_sql": """
            SELECT e.employee_id, e.full_name, d.department_name
            FROM employees e
            INNER JOIN departments d ON e.dept_id = d.dept_id;
        """,
    },
    {
        "id": "medium-3-sales-by-region",
        "title": "Total Sales Per Region",
        "difficulty": "medium",
        "tags": ["aggregation", "group-by", "null-handling"],
        "description": (
            "The `sales` table has a NULL `region` for a couple of rows "
            "(unassigned territory) and NULL `amount` for one row (refund "
            "pending). Return `region` and the total `amount` (as "
            "`total_sales`) per region, excluding NULL regions, treating "
            "NULL amounts as not contributing to the sum. Sort by "
            "`total_sales` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE sales (
                sale_id INTEGER,
                region VARCHAR,
                amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO sales VALUES
            (1, 'North', 500.00),
            (2, 'South', 300.00),
            (3, 'North', 200.00),
            (4, NULL, 150.00),
            (5, 'South', NULL),
            (6, 'East', 400.00),
            (7, 'South', 100.00);
        """,
        "canonical_sql": """
            SELECT region, SUM(amount) AS total_sales
            FROM sales
            WHERE region IS NOT NULL
            GROUP BY region
            ORDER BY total_sales DESC;
        """,
    },
    {
        "id": "medium-4-avg-order-having",
        "title": "Customers Averaging Over 200",
        "difficulty": "medium",
        "tags": ["aggregation", "group-by", "having"],
        "description": (
            "Return `customer_id` and their average order amount (as "
            "`avg_order`) from `orders`, but only for customers whose "
            "average order amount is greater than 200. Ignore NULL amounts "
            "when averaging. Sort by `avg_order` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, 1, 250.00),
            (2, 1, 150.00),
            (3, 2, 400.00),
            (4, 2, NULL),
            (5, 3, 50.00),
            (6, 3, 60.00),
            (7, 4, 900.00);
        """,
        "canonical_sql": """
            SELECT customer_id, AVG(order_amount) AS avg_order
            FROM orders
            GROUP BY customer_id
            HAVING AVG(order_amount) > 200
            ORDER BY avg_order DESC;
        """,
    },
    {
        "id": "hard-1-above-dept-average",
        "title": "Above Their Department's Average Salary",
        "difficulty": "hard",
        "tags": ["subquery", "correlated-subquery"],
        "description": (
            "Return `employee_id`, `full_name`, `department`, and `salary` "
            "for employees who earn more than the average salary of their "
            "own department. NULL salaries should be excluded entirely "
            "(not counted in the average, not returned as a result row). "
            "Sort by `department`, then `salary` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                department VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 'Engineering', 95000),
            (2, 'Vikram Shah', 'Engineering', 80000),
            (3, 'Divya Iyer', 'Engineering', 91000),
            (4, 'Priya Nair', 'Sales', 62000),
            (5, 'Karan Mehta', 'Sales', 70000),
            (6, 'Neha Joshi', 'Sales', NULL),
            (7, 'Rahul Verma', 'Sales', 58000);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, department, salary
            FROM employees e
            WHERE salary IS NOT NULL
              AND salary > (
                  SELECT AVG(salary)
                  FROM employees e2
                  WHERE e2.department = e.department AND e2.salary IS NOT NULL
              )
            ORDER BY department, salary DESC;
        """,
    },
    {
        "id": "hard-2-customers-all-products-in-category",
        "title": "Customers Who Bought Every 'Snacks' Product",
        "difficulty": "hard",
        "tags": ["subquery", "not-exists", "set-based"],
        "description": (
            "There are 3 products in the 'Snacks' category. Return the "
            "`customer_id` of every customer who has purchased ALL of them "
            "(at least once each) -- a classic relational-division problem. "
            "A customer may also buy other, non-Snacks products; that "
            "shouldn't disqualify them. Sort by `customer_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                product_name VARCHAR,
                category VARCHAR
            );
            CREATE TABLE purchases (
                purchase_id INTEGER,
                customer_id INTEGER,
                product_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips', 'Snacks'),
            (2, 'Cookies', 'Snacks'),
            (3, 'Namkeen', 'Snacks'),
            (4, 'Soda', 'Beverages');

            INSERT INTO purchases VALUES
            (1, 100, 1),
            (2, 100, 2),
            (3, 100, 3),
            (4, 100, 4),
            (5, 200, 1),
            (6, 200, 2),
            (7, 300, 1),
            (8, 300, 2),
            (9, 300, 3);
        """,
        "canonical_sql": """
            SELECT p.customer_id
            FROM purchases p
            JOIN products pr ON p.product_id = pr.product_id
            WHERE pr.category = 'Snacks'
            GROUP BY p.customer_id
            HAVING COUNT(DISTINCT pr.product_id) = (
                SELECT COUNT(*) FROM products WHERE category = 'Snacks'
            )
            ORDER BY p.customer_id;
        """,
    },
    {
        "id": "hard-3-rank-salary-in-department",
        "title": "Rank Employees by Salary Within Department",
        "difficulty": "hard",
        "tags": ["window-functions", "rank"],
        "description": (
            "Two employees in Sales are tied on salary. Using a window "
            "function, return `employee_id`, `full_name`, `department`, "
            "`salary`, and a `salary_rank` column that ranks employees by "
            "salary (highest = 1) within each department, using standard "
            "competition ranking (ties share a rank, next rank skips -- "
            "i.e. RANK(), not ROW_NUMBER() or DENSE_RANK()). Sort by "
            "`department`, then `salary_rank`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                department VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 'Engineering', 95000),
            (2, 'Vikram Shah', 'Engineering', 88000),
            (3, 'Divya Iyer', 'Engineering', 91000),
            (4, 'Priya Nair', 'Sales', 70000),
            (5, 'Karan Mehta', 'Sales', 70000),
            (6, 'Rahul Verma', 'Sales', 58000);
        """,
        "canonical_sql": """
            SELECT
                employee_id,
                full_name,
                department,
                salary,
                RANK() OVER (PARTITION BY department ORDER BY salary DESC) AS salary_rank
            FROM employees
            ORDER BY department, salary_rank;
        """,
    },
    {
        "id": "hard-4-running-total-sales",
        "title": "Running Total of Daily Sales",
        "difficulty": "hard",
        "tags": ["window-functions", "running-total"],
        "description": (
            "The `daily_sales` table has one row per date (note: two rows "
            "share the same date, 2024-01-03, from a duplicate batch load "
            "-- both should still be included). Return `sale_date`, "
            "`amount`, and a `running_total` column showing the cumulative "
            "sum of `amount` ordered by `sale_date` (and by `sale_id` to "
            "break ties on the same date). Sort by `sale_date`, then "
            "`sale_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE daily_sales (
                sale_id INTEGER,
                sale_date DATE,
                amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO daily_sales VALUES
            (1, '2024-01-01', 100.00),
            (2, '2024-01-02', 150.00),
            (3, '2024-01-03', 200.00),
            (4, '2024-01-03', 50.00),
            (5, '2024-01-04', 300.00);
        """,
        "canonical_sql": """
            SELECT
                sale_date,
                amount,
                SUM(amount) OVER (ORDER BY sale_date, sale_id
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total
            FROM daily_sales
            ORDER BY sale_date, sale_id;
        """,
    },
]


def get_problem(problem_id: str):
    for p in PROBLEMS:
        if p["id"] == problem_id:
            return p
    return None


def list_problems_summary():
    return [
        {
            "id": p["id"],
            "title": p["title"],
            "difficulty": p["difficulty"],
            "tags": p["tags"],
        }
        for p in PROBLEMS
    ]
