"""
Problem bank for the SQL practice MVP.

Problems live in Postgres (db.py's `problems` table), not this module --
this file only holds the *content* used to seed that table the first time
the app starts against an empty database, plus the query functions the
rest of the backend calls. Existing problems are never re-seeded once the
table is non-empty, so editing PROBLEMS after first deploy has no effect;
use the admin batch-generation flow (llm.generate_problem_batch) or a
direct SQL insert for anything after the initial seed.

Each problem is self-contained: its own schema, its own seed data, and a
canonical query used both to (a) compute the expected output at problem-load
time and (b) as a reference for the AI explanation prompt.

`order_matters=False` means the grader compares results as multisets (sorted
before comparing) rather than caring about row order -- most problems here
don't ask for a specific order, so getting ORDER BY "wrong" shouldn't fail you.
Problems that explicitly ask for an order (e.g. "top N by X") set it True.

Date-based problems deliberately use a fixed literal reference date instead
of CURRENT_DATE/NOW() -- the expected output is computed once and cached at
startup (see main.py's _EXPECTED_CACHE), so a canonical query tied to "today"
would silently drift out of sync with itself after the day changes.
"""

import difflib
import json
import uuid

import db
import sandbox
import topics
import py_topics
import stats_topics
import data_lib_topics
import pysandbox

# Above this title-similarity ratio, a draft is treated as a near-duplicate
# of an existing problem and rejected -- catches "Employees Earning More
# Than Their Manager" vs "Employees Who Earn More Than Their Manager"
# style near-misses that a plain string-equality check would let through.
DUPLICATE_TITLE_THRESHOLD = 0.82

PROBLEMS = [
    {
        "id": "easy-1-filter-active-employees",
        "title": "Active Employees in Engineering",
        "difficulty": "easy",
        "topic": "Retrieving Records",
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
        "topic": "Retrieving Records",
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
        "topic": "Working with Multiple Tables",
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
        "topic": "Working with Multiple Tables",
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
        "topic": "Reporting and Warehousing",
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
        "topic": "Reporting and Warehousing",
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
        "topic": "Advanced Searching",
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
        "topic": "Advanced Searching",
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
        "topic": "Reporting and Warehousing",
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
        "topic": "Reporting and Warehousing",
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

    # ---- New problems: Sorting Query Results ----
    {
        "id": "easy-3-sort-employees-by-salary",
        "title": "Employees Sorted by Salary, Nulls Last",
        "difficulty": "easy",
        "topic": "Sorting Query Results",
        "tags": ["sorting", "order-by", "null-handling"],
        "description": (
            "Some employees don't have a `salary` recorded yet (NULL). "
            "Return `employee_id`, `full_name`, and `salary` for all "
            "employees, sorted by `salary` descending -- but NULL salaries "
            "should sort LAST regardless of direction, not first (DuckDB's "
            "default NULL ordering may not already do what you want here)."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                full_name VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha Rao', 95000),
            (2, 'Vikram Shah', NULL),
            (3, 'Divya Iyer', 91000),
            (4, 'Priya Nair', 62000),
            (5, 'Karan Mehta', NULL);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, salary
            FROM employees
            ORDER BY salary DESC NULLS LAST;
        """,
    },
    {
        "id": "medium-5-sort-multi-column",
        "title": "Sort by Department, Then Salary",
        "difficulty": "medium",
        "topic": "Sorting Query Results",
        "tags": ["sorting", "order-by", "multi-column"],
        "description": (
            "Return `employee_id`, `full_name`, `department`, and `salary` "
            "for all employees, sorted by `department` ascending, and "
            "within each department by `salary` descending."
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
            (3, 'Priya Nair', 'Sales', 62000),
            (4, 'Karan Mehta', 'Sales', 70000),
            (5, 'Divya Iyer', 'Engineering', 91000);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, department, salary
            FROM employees
            ORDER BY department ASC, salary DESC;
        """,
    },

    # ---- New problems: Metadata Queries ----
    {
        "id": "medium-6-metadata-column-types",
        "title": "List Columns and Types of `orders`",
        "difficulty": "medium",
        "topic": "Metadata Queries",
        "tags": ["metadata", "information-schema"],
        "description": (
            "Query the database's own catalog (not the `orders` data "
            "itself) to return the `column_name` and `data_type` of every "
            "column in the `orders` table, in their declared order. "
            "`information_schema.columns` is a standard way to do this "
            "across most SQL databases."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                customer_id INTEGER,
                order_date DATE,
                total_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, 101, '2024-01-05', 250.00);
        """,
        "canonical_sql": """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'orders'
            ORDER BY ordinal_position;
        """,
    },
    {
        "id": "medium-7-metadata-not-null-columns",
        "title": "Find Non-Nullable Columns",
        "difficulty": "medium",
        "topic": "Metadata Queries",
        "tags": ["metadata", "information-schema"],
        "description": (
            "The `products` table declares some columns as NOT NULL. "
            "Query `information_schema.columns` to return the "
            "`column_name` of every column in `products` that does NOT "
            "allow NULL values, ordered by their declared column order."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER NOT NULL,
                product_name VARCHAR NOT NULL,
                description VARCHAR,
                price DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips', 'Salted potato chips', 40.00);
        """,
        "canonical_sql": """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'products' AND is_nullable = 'NO'
            ORDER BY ordinal_position;
        """,
    },

    # ---- New problems: Working with Strings ----
    {
        "id": "easy-4-email-domains",
        "title": "Extract Customer Email Domains",
        "difficulty": "easy",
        "topic": "Working with Strings",
        "tags": ["strings", "split-part"],
        "description": (
            "Return `customer_id` and the domain portion of each "
            "customer's `email` (everything after the `@`) as `domain`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE customers (
                customer_id INTEGER,
                email VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO customers VALUES
            (1, 'ananya@gmail.com'),
            (2, 'ravi.k@outlook.com'),
            (3, 'sales@bharattextiles.in');
        """,
        "canonical_sql": """
            SELECT customer_id, split_part(email, '@', 2) AS domain
            FROM customers;
        """,
    },
    {
        "id": "medium-8-first-word-of-product",
        "title": "First Word of Multi-Word Product Names",
        "difficulty": "medium",
        "topic": "Working with Strings",
        "tags": ["strings", "like", "split-part"],
        "description": (
            "Some `product_name` values are a single word, others are "
            "multiple words separated by spaces. Return `product_id` and "
            "the first word of `product_name` as `first_word`, but ONLY "
            "for products whose name actually contains more than one word."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                product_name VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips'),
            (2, 'Masala Chips'),
            (3, 'Soda'),
            (4, 'Diet Cola Soda');
        """,
        "canonical_sql": """
            SELECT product_id, split_part(product_name, ' ', 1) AS first_word
            FROM products
            WHERE product_name LIKE '% %';
        """,
    },

    # ---- New problems: Working with Numbers ----
    {
        "id": "easy-5-discounted-price",
        "title": "Round Prices and Compute a Discount",
        "difficulty": "easy",
        "topic": "Working with Numbers",
        "tags": ["numbers", "round", "arithmetic"],
        "description": (
            "Return `product_id`, `price` rounded to 2 decimal places, and "
            "a `discounted_price` column that's 10% off `price`, also "
            "rounded to 2 decimal places."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                price DECIMAL(10,4)
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 199.995),
            (2, 45.111),
            (3, 1000.006);
        """,
        "canonical_sql": """
            SELECT product_id, ROUND(price, 2) AS price, ROUND(price * 0.9, 2) AS discounted_price
            FROM products;
        """,
    },
    {
        "id": "medium-9-round-hundred-orders",
        "title": "Flag Orders That Are Round Hundreds",
        "difficulty": "medium",
        "topic": "Working with Numbers",
        "tags": ["numbers", "modulo", "case"],
        "description": (
            "Return `order_id`, `total_amount`, and a `category` column "
            "that says 'Round Hundred' if `total_amount` is exactly "
            "divisible by 100, otherwise 'Other'."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                total_amount INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, 500),
            (2, 450),
            (3, 1200),
            (4, 275);
        """,
        "canonical_sql": """
            SELECT order_id, total_amount,
                   CASE WHEN MOD(total_amount, 100) = 0 THEN 'Round Hundred' ELSE 'Other' END AS category
            FROM orders;
        """,
    },

    # ---- New problems: Date Arithmetic ----
    {
        "id": "easy-6-orders-in-date-window",
        "title": "Orders Placed in a 30-Day Window",
        "difficulty": "easy",
        "topic": "Date Arithmetic",
        "tags": ["dates", "between"],
        "description": (
            "Treat 2024-07-15 as \"today\" for this problem (don't use "
            "CURRENT_DATE). Return `order_id` and `order_date` for orders "
            "placed in the 30 days up to and including 2024-07-15, i.e. "
            "from 2024-06-15 onward."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-05-01'),
            (2, '2024-06-20'),
            (3, '2024-07-01'),
            (4, '2024-07-15'),
            (5, '2024-08-01');
        """,
        "canonical_sql": """
            SELECT order_id, order_date
            FROM orders
            WHERE order_date BETWEEN DATE '2024-06-15' AND DATE '2024-07-15';
        """,
    },
    {
        "id": "medium-10-days-to-ship",
        "title": "Days Between Order and Shipment",
        "difficulty": "medium",
        "topic": "Date Arithmetic",
        "tags": ["dates", "arithmetic"],
        "description": (
            "Some orders haven't shipped yet (`ship_date` is NULL). Return "
            "`order_id` and a `days_to_ship` column: the number of days "
            "between `order_date` and `ship_date`. Leave `days_to_ship` as "
            "NULL for orders that haven't shipped."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE,
                ship_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-06-01', '2024-06-04'),
            (2, '2024-06-10', '2024-06-10'),
            (3, '2024-06-15', NULL),
            (4, '2024-06-20', '2024-06-25');
        """,
        "canonical_sql": """
            SELECT order_id, (ship_date - order_date) AS days_to_ship
            FROM orders;
        """,
    },

    # ---- New problems: Date Manipulation ----
    {
        "id": "easy-7-truncate-to-month",
        "title": "Bucket Orders by Month",
        "difficulty": "easy",
        "topic": "Date Manipulation",
        "tags": ["dates", "date-trunc"],
        "description": (
            "Return `order_id` and an `order_month` column: `order_date` "
            "truncated to the first day of its month."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-06-05'),
            (2, '2024-06-27'),
            (3, '2024-07-02');
        """,
        "canonical_sql": """
            SELECT order_id, DATE_TRUNC('month', order_date) AS order_month
            FROM orders;
        """,
    },
    {
        "id": "medium-11-return-deadline",
        "title": "Compute a Return Deadline",
        "difficulty": "medium",
        "topic": "Date Manipulation",
        "tags": ["dates", "interval"],
        "description": (
            "Orders can be returned within 7 days of shipping. Return "
            "`order_id` and a `return_deadline` column: `ship_date` plus 7 "
            "days. Orders with a NULL `ship_date` should have a NULL "
            "`return_deadline`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                ship_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-06-01'),
            (2, '2024-06-15'),
            (3, NULL);
        """,
        "canonical_sql": """
            SELECT order_id, ship_date + INTERVAL 7 DAY AS return_deadline
            FROM orders;
        """,
    },

    # ---- New problems: Working with Ranges ----
    {
        "id": "easy-8-salary-bands",
        "title": "Bucket Employees Into Salary Bands",
        "difficulty": "easy",
        "topic": "Working with Ranges",
        "tags": ["ranges", "case", "between"],
        "description": (
            "Return `employee_id`, `salary`, and a `band` column: "
            "'Low' for salary under 50000, 'Mid' for 50000 to 90000 "
            "inclusive, and 'High' for anything above 90000."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 45000),
            (2, 62000),
            (3, 90000),
            (4, 120000),
            (5, 55000);
        """,
        "canonical_sql": """
            SELECT employee_id, salary,
                   CASE
                       WHEN salary < 50000 THEN 'Low'
                       WHEN salary BETWEEN 50000 AND 90000 THEN 'Mid'
                       ELSE 'High'
                   END AS band
            FROM employees;
        """,
    },
    {
        "id": "medium-12-overlapping-bookings",
        "title": "Find Overlapping Room Bookings",
        "difficulty": "medium",
        "topic": "Working with Ranges",
        "tags": ["ranges", "date-overlap"],
        "description": (
            "Return the `booking_id` of every booking for `room_id` 101 "
            "whose date range overlaps at all with 2024-07-01 through "
            "2024-07-10 inclusive (a booking overlaps if it starts on or "
            "before the window ends, AND ends on or after the window "
            "starts)."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE bookings (
                booking_id INTEGER,
                room_id INTEGER,
                start_date DATE,
                end_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO bookings VALUES
            (1, 101, '2024-06-25', '2024-06-30'),
            (2, 101, '2024-07-05', '2024-07-08'),
            (3, 101, '2024-07-09', '2024-07-15'),
            (4, 101, '2024-07-11', '2024-07-20'),
            (5, 102, '2024-07-05', '2024-07-08');
        """,
        "canonical_sql": """
            SELECT booking_id
            FROM bookings
            WHERE room_id = 101
              AND start_date <= DATE '2024-07-10'
              AND end_date >= DATE '2024-07-01'
            ORDER BY booking_id;
        """,
    },

    # ---- New problems: Hierarchical Queries ----
    {
        "id": "medium-13-employee-manager-name",
        "title": "Employees With Their Manager's Name",
        "difficulty": "medium",
        "topic": "Hierarchical Queries",
        "tags": ["self-join", "hierarchy"],
        "description": (
            "The `employees` table is self-referential: `manager_id` "
            "points to another row's `employee_id` (NULL for the CEO, who "
            "has no manager). Return `employee_id`, `name`, and "
            "`manager_name` (NULL if they have no manager)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                name VARCHAR,
                manager_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Meera CEO', NULL),
            (2, 'Asha Rao', 1),
            (3, 'Vikram Shah', 1),
            (4, 'Divya Iyer', 2),
            (5, 'Karan Mehta', 2);
        """,
        "canonical_sql": """
            SELECT e.employee_id, e.name, m.name AS manager_name
            FROM employees e
            LEFT JOIN employees m ON e.manager_id = m.employee_id;
        """,
    },
    {
        "id": "hard-5-all-reports-under-manager",
        "title": "Every Employee Under a Manager, Any Depth",
        "difficulty": "hard",
        "topic": "Hierarchical Queries",
        "tags": ["recursive-cte", "hierarchy"],
        "description": (
            "Using a recursive CTE, return every employee (at any depth) "
            "who reports up to employee_id 1 (Meera, the CEO), whether "
            "directly or through a chain of managers. Return "
            "`employee_id`, `name`, and `level` (1 for direct reports of "
            "the CEO, 2 for their reports, and so on). Sort by `level`, "
            "then `employee_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                name VARCHAR,
                manager_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Meera CEO', NULL),
            (2, 'Asha Rao', 1),
            (3, 'Vikram Shah', 1),
            (4, 'Divya Iyer', 2),
            (5, 'Karan Mehta', 2),
            (6, 'Priya Nair', 4);
        """,
        "canonical_sql": """
            WITH RECURSIVE subordinates AS (
                SELECT employee_id, name, manager_id, 1 AS level
                FROM employees
                WHERE manager_id = 1
                UNION ALL
                SELECT e.employee_id, e.name, e.manager_id, s.level + 1
                FROM employees e
                JOIN subordinates s ON e.manager_id = s.employee_id
            )
            SELECT employee_id, name, level
            FROM subordinates
            ORDER BY level, employee_id;
        """,
    },

    # ---- New problems: Odds and Ends ----
    {
        "id": "easy-9-distinct-customer-product-pairs",
        "title": "Distinct Customer-Product Purchase Pairs",
        "difficulty": "easy",
        "topic": "Odds and Ends",
        "tags": ["distinct", "duplicates"],
        "description": (
            "The `purchases` table has duplicate rows where the same "
            "customer bought the same product more than once (logged as "
            "separate rows). Return the distinct `(customer_id, "
            "product_id)` pairs, one row per pair no matter how many times "
            "it was actually purchased."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE purchases (
                purchase_id INTEGER,
                customer_id INTEGER,
                product_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO purchases VALUES
            (1, 100, 1),
            (2, 100, 1),
            (3, 100, 2),
            (4, 200, 1),
            (5, 200, 1),
            (6, 200, 1);
        """,
        "canonical_sql": """
            SELECT DISTINCT customer_id, product_id
            FROM purchases;
        """,
    },
    {
        "id": "medium-14-pivot-quarterly-sales",
        "title": "Pivot Sales Into Quarter Columns",
        "difficulty": "medium",
        "topic": "Odds and Ends",
        "tags": ["pivot", "conditional-aggregation"],
        "description": (
            "The `sales` table has one row per (region, quarter, amount). "
            "Return one row per `region` with two columns, `q1_total` and "
            "`q2_total`, summing `amount` for 'Q1' and 'Q2' respectively "
            "(0 if a region had no sales in that quarter). Sort by "
            "`region`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE sales (
                sale_id INTEGER,
                region VARCHAR,
                quarter VARCHAR,
                amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO sales VALUES
            (1, 'North', 'Q1', 500.00),
            (2, 'North', 'Q2', 300.00),
            (3, 'South', 'Q1', 200.00),
            (4, 'East', 'Q2', 400.00);
        """,
        "canonical_sql": """
            SELECT region,
                   SUM(CASE WHEN quarter = 'Q1' THEN amount ELSE 0 END) AS q1_total,
                   SUM(CASE WHEN quarter = 'Q2' THEN amount ELSE 0 END) AS q2_total
            FROM sales
            GROUP BY region
            ORDER BY region;
        """,
    },

    # ==================== Batch 2 ====================

    # ---- Retrieving Records ----
    {
        "id": "easy-10-library-available-fiction",
        "title": "Available Fiction Books",
        "difficulty": "easy",
        "topic": "Retrieving Records",
        "tags": ["select", "where", "like"],
        "description": (
            "Return `book_id` and `title` for books whose `genre` starts "
            "with 'Fic' (matches 'Fiction' and similar) AND are currently "
            "`available`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE books (
                book_id INTEGER,
                title VARCHAR,
                genre VARCHAR,
                available BOOLEAN
            );
        """,
        "seed_sql": """
            INSERT INTO books VALUES
            (1, 'The Silent Patient', 'Fiction', TRUE),
            (2, 'A Brief History of Time', 'Non-Fiction', TRUE),
            (3, 'Speculative Realms', 'Fictional Sagas', TRUE),
            (4, 'Dune', 'Fiction', FALSE),
            (5, 'Cosmos', 'Science', TRUE);
        """,
        "canonical_sql": """
            SELECT book_id, title
            FROM books
            WHERE genre LIKE 'Fic%' AND available = TRUE;
        """,
    },
    {
        "id": "medium-15-and-or-precedence",
        "title": "Premium or Long-Tenured Members Only",
        "difficulty": "medium",
        "topic": "Retrieving Records",
        "tags": ["where", "and-or", "operator-precedence"],
        "description": (
            "Return `member_id`, `plan`, and `years_active` for gym "
            "members who are either on the 'Premium' plan, OR have been "
            "active for more than 3 years AND are on the 'Standard' plan. "
            "(This is a classic AND/OR precedence trap -- think carefully "
            "about where parentheses need to go.)"
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE members (
                member_id INTEGER,
                plan VARCHAR,
                years_active INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO members VALUES
            (1, 'Premium', 1),
            (2, 'Standard', 5),
            (3, 'Standard', 2),
            (4, 'Basic', 6),
            (5, 'Premium', 4);
        """,
        "canonical_sql": """
            SELECT member_id, plan, years_active
            FROM members
            WHERE plan = 'Premium' OR (years_active > 3 AND plan = 'Standard');
        """,
    },
    {
        "id": "medium-16-not-in-null-trap",
        "title": "Products Not in a Discontinued List (NULL Trap)",
        "difficulty": "medium",
        "topic": "Retrieving Records",
        "tags": ["not-in", "null-handling", "subquery"],
        "description": (
            "The `discontinued_codes` table lists product codes that have "
            "been discontinued -- but one row has a NULL `code` (a bad "
            "data entry). Return `product_id` and `code` for products "
            "whose `code` is NOT in the discontinued list. Careful: NOT "
            "IN against a list containing NULL silently returns zero rows "
            "in standard SQL unless you filter the NULL out first."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                code VARCHAR
            );
            CREATE TABLE discontinued_codes (
                code VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'A100'),
            (2, 'A200'),
            (3, 'A300'),
            (4, 'A400');

            INSERT INTO discontinued_codes VALUES
            ('A200'),
            (NULL);
        """,
        "canonical_sql": """
            SELECT product_id, code
            FROM products
            WHERE code NOT IN (SELECT code FROM discontinued_codes WHERE code IS NOT NULL);
        """,
    },

    # ---- Sorting Query Results ----
    {
        "id": "medium-17-sort-nulls-first",
        "title": "Newest Reviews First, Unrated Products First",
        "difficulty": "medium",
        "topic": "Sorting Query Results",
        "tags": ["sorting", "nulls-first"],
        "description": (
            "Return `product_id` and `rating` for all products, sorted so "
            "that products with no rating yet (NULL) come FIRST, then the "
            "rest sorted by `rating` descending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                rating DECIMAL(2,1)
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 4.5),
            (2, NULL),
            (3, 3.8),
            (4, NULL),
            (5, 4.9);
        """,
        "canonical_sql": """
            SELECT product_id, rating
            FROM products
            ORDER BY rating DESC NULLS FIRST;
        """,
    },
    {
        "id": "easy-11-pagination-limit-offset",
        "title": "Second Page of Products, 2 Per Page",
        "difficulty": "easy",
        "topic": "Sorting Query Results",
        "tags": ["sorting", "limit", "offset"],
        "description": (
            "Return `product_id` and `product_name`, sorted by "
            "`product_id` ascending, showing the SECOND page of results "
            "when displaying 2 products per page (i.e. skip the first 2, "
            "return the next 2)."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                product_name VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips'),
            (2, 'Soda'),
            (3, 'Cookies'),
            (4, 'Namkeen'),
            (5, 'Juice');
        """,
        "canonical_sql": """
            SELECT product_id, product_name
            FROM products
            ORDER BY product_id
            LIMIT 2 OFFSET 2;
        """,
    },
    {
        "id": "medium-18-sort-by-expression",
        "title": "Sort by Total Price, Not a Selected Column",
        "difficulty": "medium",
        "topic": "Sorting Query Results",
        "tags": ["sorting", "computed-column"],
        "description": (
            "Return `order_id`, `quantity`, and `unit_price` for all "
            "orders, sorted descending by `quantity * unit_price` (the "
            "total line value) -- even though that computed value itself "
            "isn't one of the returned columns."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE order_items (
                order_id INTEGER,
                quantity INTEGER,
                unit_price DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO order_items VALUES
            (1, 3, 100.00),
            (2, 1, 500.00),
            (3, 10, 20.00),
            (4, 2, 150.00);
        """,
        "canonical_sql": """
            SELECT order_id, quantity, unit_price
            FROM order_items
            ORDER BY quantity * unit_price DESC;
        """,
    },

    # ---- Working with Multiple Tables ----
    {
        "id": "medium-19-three-way-join",
        "title": "Order Line Items With Customer and Product Names",
        "difficulty": "medium",
        "topic": "Working with Multiple Tables",
        "tags": ["joins", "three-table-join"],
        "description": (
            "Join `order_items`, `orders`, `customers`, and `products` "
            "together to return `order_id`, `customer_name`, "
            "`product_name`, and `quantity` for every line item."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE customers (customer_id INTEGER, customer_name VARCHAR);
            CREATE TABLE products (product_id INTEGER, product_name VARCHAR);
            CREATE TABLE orders (order_id INTEGER, customer_id INTEGER);
            CREATE TABLE order_items (order_id INTEGER, product_id INTEGER, quantity INTEGER);
        """,
        "seed_sql": """
            INSERT INTO customers VALUES (1, 'Ananya Traders'), (2, 'Bharat Textiles');
            INSERT INTO products VALUES (10, 'Chips'), (20, 'Soda');
            INSERT INTO orders VALUES (100, 1), (101, 2);
            INSERT INTO order_items VALUES (100, 10, 3), (100, 20, 1), (101, 10, 5);
        """,
        "canonical_sql": """
            SELECT o.order_id, c.customer_name, p.product_name, oi.quantity
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN customers c ON o.customer_id = c.customer_id
            JOIN products p ON oi.product_id = p.product_id;
        """,
    },
    {
        "id": "medium-20-self-join-same-category",
        "title": "Pairs of Products in the Same Category",
        "difficulty": "medium",
        "topic": "Working with Multiple Tables",
        "tags": ["self-join"],
        "description": (
            "Using a self-join on `products`, return pairs of DIFFERENT "
            "products (`product_a`, `product_b`) that share the same "
            "`category`. Each pair should appear only once (not twice in "
            "reverse order) -- use the product IDs to break symmetry."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                product_name VARCHAR,
                category VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'Chips', 'Snacks'),
            (2, 'Cookies', 'Snacks'),
            (3, 'Namkeen', 'Snacks'),
            (4, 'Soda', 'Beverages');
        """,
        "canonical_sql": """
            SELECT a.product_name AS product_a, b.product_name AS product_b
            FROM products a
            JOIN products b ON a.category = b.category AND a.product_id < b.product_id;
        """,
    },
    {
        "id": "hard-6-full-outer-join-unmatched",
        "title": "Students and Enrollments, Fully Unmatched Both Ways",
        "difficulty": "hard",
        "topic": "Working with Multiple Tables",
        "tags": ["full-join", "outer-join"],
        "description": (
            "Some students haven't enrolled in any course, and one "
            "enrollment references a `student_id` that doesn't exist in "
            "`students` (orphaned data). Using a FULL OUTER JOIN, return "
            "`student_id`, `student_name`, and `course` for every row from "
            "both sides, matched or not (NULL where there's no match)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE students (student_id INTEGER, student_name VARCHAR);
            CREATE TABLE enrollments (student_id INTEGER, course VARCHAR);
        """,
        "seed_sql": """
            INSERT INTO students VALUES (1, 'Asha'), (2, 'Vikram'), (3, 'Priya');
            INSERT INTO enrollments VALUES (1, 'SQL 101'), (2, 'Data Structures'), (99, 'Ghost Course');
        """,
        "canonical_sql": """
            SELECT s.student_id, s.student_name, e.course
            FROM students s
            FULL OUTER JOIN enrollments e ON s.student_id = e.student_id;
        """,
    },

    # ---- Metadata Queries ----
    {
        "id": "easy-12-metadata-table-exists",
        "title": "Confirm a Table Exists in the Schema",
        "difficulty": "easy",
        "topic": "Metadata Queries",
        "tags": ["metadata", "information-schema"],
        "description": (
            "Query `information_schema.tables` to return the "
            "`table_name` of every table in the current schema whose name "
            "starts with 'emp'."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (employee_id INTEGER);
            CREATE TABLE employee_history (employee_id INTEGER);
            CREATE TABLE departments (dept_id INTEGER);
        """,
        "seed_sql": """
            INSERT INTO employees VALUES (1);
        """,
        "canonical_sql": """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name LIKE 'emp%';
        """,
    },
    {
        "id": "medium-21-metadata-count-columns",
        "title": "Count Columns Per Table",
        "difficulty": "medium",
        "topic": "Metadata Queries",
        "tags": ["metadata", "information-schema", "aggregation"],
        "description": (
            "Query `information_schema.columns` to return `table_name` "
            "and the number of columns it has (as `column_count`), for "
            "every table in the schema. Sort by `table_name`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (employee_id INTEGER, name VARCHAR, salary INTEGER);
            CREATE TABLE departments (dept_id INTEGER, department_name VARCHAR);
        """,
        "seed_sql": """
            INSERT INTO employees VALUES (1, 'Asha', 90000);
        """,
        "canonical_sql": """
            SELECT table_name, COUNT(*) AS column_count
            FROM information_schema.columns
            WHERE table_name IN ('employees', 'departments')
            GROUP BY table_name
            ORDER BY table_name;
        """,
    },

    # ---- Working with Strings ----
    {
        "id": "medium-22-string-full-name-concat",
        "title": "Concatenate First and Last Name",
        "difficulty": "medium",
        "topic": "Working with Strings",
        "tags": ["strings", "concat"],
        "description": (
            "Some `last_name` values are NULL (single-name records). "
            "Return `employee_id` and a `full_name` column combining "
            "`first_name` and `last_name` with a space between them -- "
            "when `last_name` is NULL, `full_name` should just be the "
            "first name (no trailing space, no NULL result)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                first_name VARCHAR,
                last_name VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Asha', 'Rao'),
            (2, 'Cher', NULL),
            (3, 'Vikram', 'Shah');
        """,
        "canonical_sql": """
            SELECT employee_id,
                   TRIM(first_name || ' ' || COALESCE(last_name, '')) AS full_name
            FROM employees;
        """,
    },
    {
        "id": "easy-13-string-upper-category",
        "title": "Uppercase Product Categories",
        "difficulty": "easy",
        "topic": "Working with Strings",
        "tags": ["strings", "upper"],
        "description": (
            "Return `product_id` and `category` converted to all "
            "uppercase, as `category_upper`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                category VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'snacks'),
            (2, 'Beverages'),
            (3, 'dairy');
        """,
        "canonical_sql": """
            SELECT product_id, UPPER(category) AS category_upper
            FROM products;
        """,
    },
    {
        "id": "medium-23-string-length-filter",
        "title": "Product Codes Longer Than 5 Characters",
        "difficulty": "medium",
        "topic": "Working with Strings",
        "tags": ["strings", "length"],
        "description": (
            "Return `product_id` and `code` for products whose `code` is "
            "longer than 5 characters."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE products (
                product_id INTEGER,
                code VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO products VALUES
            (1, 'A100'),
            (2, 'PROD-2024'),
            (3, 'B22'),
            (4, 'SKU-99001');
        """,
        "canonical_sql": """
            SELECT product_id, code
            FROM products
            WHERE LENGTH(code) > 5;
        """,
    },

    # ---- Working with Numbers ----
    {
        "id": "easy-14-numbers-absolute-delta",
        "title": "Absolute Value of Account Balance Change",
        "difficulty": "easy",
        "topic": "Working with Numbers",
        "tags": ["numbers", "abs"],
        "description": (
            "Return `transaction_id` and the absolute value of `delta` "
            "(which can be negative for withdrawals) as `magnitude`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE transactions (
                transaction_id INTEGER,
                delta DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO transactions VALUES
            (1, 500.00),
            (2, -200.50),
            (3, -75.00),
            (4, 1000.00);
        """,
        "canonical_sql": """
            SELECT transaction_id, ABS(delta) AS magnitude
            FROM transactions;
        """,
    },
    {
        "id": "medium-24-numbers-ceil-billing-units",
        "title": "Round Up Data Usage to Whole Billing Units",
        "difficulty": "medium",
        "topic": "Working with Numbers",
        "tags": ["numbers", "ceiling"],
        "description": (
            "A billing unit is 1 GB; partial usage still counts as a full "
            "unit. Return `customer_id` and `gb_used` rounded UP to the "
            "next whole number as `billed_units` (e.g. 2.1 GB bills as 3 "
            "units)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE usage (
                customer_id INTEGER,
                gb_used DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO usage VALUES
            (1, 2.1),
            (2, 5.0),
            (3, 0.3),
            (4, 9.99);
        """,
        "canonical_sql": """
            SELECT customer_id, CEIL(gb_used) AS billed_units
            FROM usage;
        """,
    },
    {
        "id": "medium-25-numbers-percent-of-total",
        "title": "Each Region's Share of Total Revenue",
        "difficulty": "medium",
        "topic": "Working with Numbers",
        "tags": ["numbers", "arithmetic", "subquery"],
        "description": (
            "Return `region` and `revenue`, plus a `pct_of_total` column: "
            "each region's revenue as a percentage of the sum of ALL "
            "regions' revenue, rounded to 1 decimal place."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE region_revenue (
                region VARCHAR,
                revenue DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO region_revenue VALUES
            ('North', 500.00),
            ('South', 300.00),
            ('East', 200.00);
        """,
        "canonical_sql": """
            SELECT region, revenue,
                   ROUND(revenue * 100.0 / (SELECT SUM(revenue) FROM region_revenue), 1) AS pct_of_total
            FROM region_revenue;
        """,
    },

    # ---- Date Arithmetic ----
    {
        "id": "easy-15-date-age-in-years",
        "title": "Patient Age in Years",
        "difficulty": "easy",
        "topic": "Date Arithmetic",
        "tags": ["dates", "date-diff"],
        "description": (
            "Treat 2024-07-15 as \"today\" (don't use CURRENT_DATE). "
            "Return `patient_id` and `age_years`: the number of full "
            "years between `birth_date` and 2024-07-15."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE patients (
                patient_id INTEGER,
                birth_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO patients VALUES
            (1, '1990-03-10'),
            (2, '2000-12-25'),
            (3, '1965-07-16');
        """,
        "canonical_sql": """
            SELECT patient_id, DATE_DIFF('year', birth_date, DATE '2024-07-15') AS age_years
            FROM patients;
        """,
    },
    {
        "id": "medium-26-date-subscription-expiry",
        "title": "Subscription Expiry Date",
        "difficulty": "medium",
        "topic": "Date Arithmetic",
        "tags": ["dates", "interval"],
        "description": (
            "Each subscription lasts `plan_months` months from "
            "`start_date`. Return `subscription_id` and an "
            "`expiry_date` column computed accordingly."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE subscriptions (
                subscription_id INTEGER,
                start_date DATE,
                plan_months INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO subscriptions VALUES
            (1, '2024-01-15', 1),
            (2, '2024-02-01', 12),
            (3, '2024-06-10', 3);
        """,
        "canonical_sql": """
            SELECT subscription_id, start_date + (plan_months * INTERVAL 1 MONTH) AS expiry_date
            FROM subscriptions;
        """,
    },
    {
        "id": "medium-27-date-weekend-orders",
        "title": "Orders Placed on a Weekend",
        "difficulty": "medium",
        "topic": "Date Arithmetic",
        "tags": ["dates", "dayofweek"],
        "description": (
            "Return `order_id` and `order_date` for orders placed on a "
            "Saturday or Sunday."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-07-08'),
            (2, '2024-07-13'),
            (3, '2024-07-14'),
            (4, '2024-07-10');
        """,
        "canonical_sql": """
            SELECT order_id, order_date
            FROM orders
            WHERE DAYOFWEEK(order_date) IN (0, 6);
        """,
    },

    # ---- Date Manipulation ----
    {
        "id": "easy-16-date-extract-year",
        "title": "Extract the Year From a Hire Date",
        "difficulty": "easy",
        "topic": "Date Manipulation",
        "tags": ["dates", "extract"],
        "description": (
            "Return `employee_id` and a `hire_year` column: the year "
            "portion of `hire_date`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                hire_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, '2019-05-10'),
            (2, '2021-11-02'),
            (3, '2023-01-30');
        """,
        "canonical_sql": """
            SELECT employee_id, EXTRACT(YEAR FROM hire_date) AS hire_year
            FROM employees;
        """,
    },
    {
        "id": "medium-28-date-last-day-of-month",
        "title": "Last Day of the Billing Month",
        "difficulty": "medium",
        "topic": "Date Manipulation",
        "tags": ["dates", "last-day"],
        "description": (
            "Return `invoice_id` and a `billing_month_end` column: the "
            "last calendar day of the month that `invoice_date` falls in."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE invoices (
                invoice_id INTEGER,
                invoice_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO invoices VALUES
            (1, '2024-02-10'),
            (2, '2024-04-01'),
            (3, '2024-12-25');
        """,
        "canonical_sql": """
            SELECT invoice_id, LAST_DAY(invoice_date) AS billing_month_end
            FROM invoices;
        """,
    },
    {
        "id": "medium-29-date-format-string",
        "title": "Format Order Dates as DD-Mon-YYYY",
        "difficulty": "medium",
        "topic": "Date Manipulation",
        "tags": ["dates", "strftime", "formatting"],
        "description": (
            "Return `order_id` and a `formatted_date` column: "
            "`order_date` formatted as e.g. '05-Jul-2024' (day, "
            "3-letter month abbreviation, 4-digit year)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                order_date DATE
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, '2024-07-05'),
            (2, '2024-01-20');
        """,
        "canonical_sql": """
            SELECT order_id, STRFTIME(order_date, '%d-%b-%Y') AS formatted_date
            FROM orders;
        """,
    },

    # ---- Working with Ranges ----
    {
        "id": "easy-17-ranges-age-groups",
        "title": "Bucket Customers Into Age Groups",
        "difficulty": "easy",
        "topic": "Working with Ranges",
        "tags": ["ranges", "case"],
        "description": (
            "Return `customer_id`, `age`, and an `age_group` column: "
            "'Minor' under 18, 'Adult' 18-59 inclusive, 'Senior' 60+."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE customers (
                customer_id INTEGER,
                age INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO customers VALUES
            (1, 15),
            (2, 34),
            (3, 60),
            (4, 72),
            (5, 17);
        """,
        "canonical_sql": """
            SELECT customer_id, age,
                   CASE
                       WHEN age < 18 THEN 'Minor'
                       WHEN age BETWEEN 18 AND 59 THEN 'Adult'
                       ELSE 'Senior'
                   END AS age_group
            FROM customers;
        """,
    },
    {
        "id": "medium-30-ranges-grade-bands",
        "title": "Assign Letter Grades From Scores",
        "difficulty": "medium",
        "topic": "Working with Ranges",
        "tags": ["ranges", "case", "between"],
        "description": (
            "Return `student_id`, `score`, and a `grade` column: 'A' for "
            "90-100, 'B' for 80-89, 'C' for 70-79, 'F' below 70."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE scores (
                student_id INTEGER,
                score INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO scores VALUES
            (1, 95),
            (2, 82),
            (3, 71),
            (4, 55),
            (5, 89);
        """,
        "canonical_sql": """
            SELECT student_id, score,
                   CASE
                       WHEN score BETWEEN 90 AND 100 THEN 'A'
                       WHEN score BETWEEN 80 AND 89 THEN 'B'
                       WHEN score BETWEEN 70 AND 79 THEN 'C'
                       ELSE 'F'
                   END AS grade
            FROM scores;
        """,
    },
    {
        "id": "hard-7-ranges-gaps-in-sequence",
        "title": "Find Missing Invoice Numbers",
        "difficulty": "hard",
        "topic": "Working with Ranges",
        "tags": ["ranges", "gaps-and-islands", "recursive-cte"],
        "description": (
            "Invoice numbers should be sequential from 1 to the highest "
            "one issued, with no gaps -- but some are missing (voided and "
            "never reused). Return the `invoice_number` of every missing "
            "number in that range, sorted ascending."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE invoices (
                invoice_number INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO invoices VALUES
            (1), (2), (4), (5), (8);
        """,
        "canonical_sql": """
            WITH RECURSIVE seq(n) AS (
                SELECT 1
                UNION ALL
                SELECT n + 1 FROM seq WHERE n < (SELECT MAX(invoice_number) FROM invoices)
            )
            SELECT n AS invoice_number
            FROM seq
            WHERE n NOT IN (SELECT invoice_number FROM invoices)
            ORDER BY invoice_number;
        """,
    },

    # ---- Advanced Searching ----
    {
        "id": "medium-31-advanced-exists",
        "title": "Customers With at Least One Big Order",
        "difficulty": "medium",
        "topic": "Advanced Searching",
        "tags": ["exists", "subquery"],
        "description": (
            "Using EXISTS, return `customer_id` and `customer_name` for "
            "customers who have placed at least one order over 1000."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE customers (customer_id INTEGER, customer_name VARCHAR);
            CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, amount DECIMAL(10,2));
        """,
        "seed_sql": """
            INSERT INTO customers VALUES (1, 'Ananya Traders'), (2, 'Bharat Textiles'), (3, 'Chennai Foods');
            INSERT INTO orders VALUES (100, 1, 1500.00), (101, 1, 200.00), (102, 2, 300.00), (103, 3, 50.00);
        """,
        "canonical_sql": """
            SELECT c.customer_id, c.customer_name
            FROM customers c
            WHERE EXISTS (
                SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id AND o.amount > 1000
            );
        """,
    },
    {
        "id": "hard-8-advanced-greater-than-all",
        "title": "Employees Earning More Than Every Sales Rep",
        "difficulty": "hard",
        "topic": "Advanced Searching",
        "tags": ["subquery", "all"],
        "description": (
            "Return `employee_id`, `full_name`, `department`, and "
            "`salary` for employees who earn more than EVERY employee in "
            "the 'Sales' department (using `> ALL`, not MAX)."
        ),
        "order_matters": False,
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
            (1, 'Asha Rao', 'Engineering', 120000),
            (2, 'Vikram Shah', 'Engineering', 75000),
            (3, 'Priya Nair', 'Sales', 62000),
            (4, 'Karan Mehta', 'Sales', 70000),
            (5, 'Divya Iyer', 'Marketing', 95000);
        """,
        "canonical_sql": """
            SELECT employee_id, full_name, department, salary
            FROM employees
            WHERE salary > ALL (SELECT salary FROM employees WHERE department = 'Sales');
        """,
    },
    {
        "id": "hard-9-advanced-highest-avg-department",
        "title": "The Department With the Highest Average Salary",
        "difficulty": "hard",
        "topic": "Advanced Searching",
        "tags": ["subquery", "aggregation", "correlated-subquery"],
        "description": (
            "Return the single `department` with the highest average "
            "salary (just the department name, one row)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                department VARCHAR,
                salary INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Engineering', 95000),
            (2, 'Engineering', 88000),
            (3, 'Sales', 62000),
            (4, 'Sales', 70000),
            (5, 'Marketing', 120000);
        """,
        "canonical_sql": """
            SELECT department
            FROM employees
            GROUP BY department
            ORDER BY AVG(salary) DESC
            LIMIT 1;
        """,
    },

    # ---- Reporting and Warehousing ----
    {
        "id": "medium-32-reporting-count-distinct-buyers",
        "title": "Distinct Buyers Per Product",
        "difficulty": "medium",
        "topic": "Reporting and Warehousing",
        "tags": ["aggregation", "count-distinct"],
        "description": (
            "The `purchases` table can have the same customer buying the "
            "same product more than once. Return `product_id` and the "
            "number of DISTINCT customers who bought it, as `buyer_count`."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE purchases (
                purchase_id INTEGER,
                customer_id INTEGER,
                product_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO purchases VALUES
            (1, 100, 1), (2, 100, 1), (3, 200, 1), (4, 300, 2);
        """,
        "canonical_sql": """
            SELECT product_id, COUNT(DISTINCT customer_id) AS buyer_count
            FROM purchases
            GROUP BY product_id;
        """,
    },
    {
        "id": "hard-10-reporting-month-over-month",
        "title": "Month-Over-Month Revenue Change",
        "difficulty": "hard",
        "topic": "Reporting and Warehousing",
        "tags": ["window-functions", "lag"],
        "description": (
            "Return `month`, `revenue`, and a `change_from_prior_month` "
            "column using LAG() -- the difference between this month's "
            "revenue and the previous month's (NULL for the first month)."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE monthly_revenue (
                month DATE,
                revenue DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO monthly_revenue VALUES
            ('2024-01-01', 1000.00),
            ('2024-02-01', 1200.00),
            ('2024-03-01', 900.00);
        """,
        "canonical_sql": """
            SELECT month, revenue,
                   revenue - LAG(revenue) OVER (ORDER BY month) AS change_from_prior_month
            FROM monthly_revenue
            ORDER BY month;
        """,
    },
    {
        "id": "hard-11-reporting-ntile-quartiles",
        "title": "Bucket Customers Into Spending Quartiles",
        "difficulty": "hard",
        "topic": "Reporting and Warehousing",
        "tags": ["window-functions", "ntile"],
        "description": (
            "Return `customer_id`, `total_spent`, and a `quartile` "
            "column using NTILE(4) -- bucketing customers into 4 roughly "
            "equal groups by `total_spent` descending (1 = top spenders)."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE customer_spend (
                customer_id INTEGER,
                total_spent DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO customer_spend VALUES
            (1, 5000.00), (2, 4000.00), (3, 3000.00), (4, 2000.00),
            (5, 1000.00), (6, 900.00), (7, 800.00), (8, 700.00);
        """,
        "canonical_sql": """
            SELECT customer_id, total_spent,
                   NTILE(4) OVER (ORDER BY total_spent DESC) AS quartile
            FROM customer_spend
            ORDER BY total_spent DESC;
        """,
    },

    # ---- Hierarchical Queries ----
    {
        "id": "medium-33-hierarchy-root-nodes",
        "title": "Employees With No Manager",
        "difficulty": "medium",
        "topic": "Hierarchical Queries",
        "tags": ["hierarchy", "self-reference"],
        "description": (
            "Return `employee_id` and `name` for employees who have no "
            "manager (the top of the org chart -- `manager_id` is NULL)."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                name VARCHAR,
                manager_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, 'Meera CEO', NULL),
            (2, 'Asha Rao', 1),
            (3, 'Vikram Shah', 1);
        """,
        "canonical_sql": """
            SELECT employee_id, name
            FROM employees
            WHERE manager_id IS NULL;
        """,
    },
    {
        "id": "hard-12-hierarchy-count-descendants",
        "title": "Total Headcount Under Each Manager",
        "difficulty": "hard",
        "topic": "Hierarchical Queries",
        "tags": ["recursive-cte", "hierarchy", "aggregation"],
        "description": (
            "Using a recursive CTE, return `manager_id` and the TOTAL "
            "number of employees reporting up to them at any depth (not "
            "just direct reports), as `total_reports`. Only include "
            "managers who have at least one report. Sort by `manager_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE employees (
                employee_id INTEGER,
                manager_id INTEGER
            );
        """,
        "seed_sql": """
            INSERT INTO employees VALUES
            (1, NULL),
            (2, 1),
            (3, 1),
            (4, 2),
            (5, 2),
            (6, 4);
        """,
        "canonical_sql": """
            WITH RECURSIVE reports AS (
                SELECT employee_id AS manager_id, employee_id AS report_id
                FROM employees
                UNION ALL
                SELECT r.manager_id, e.employee_id
                FROM employees e
                JOIN reports r ON e.manager_id = r.report_id
            )
            SELECT manager_id, COUNT(*) - 1 AS total_reports
            FROM reports
            GROUP BY manager_id
            HAVING COUNT(*) - 1 > 0
            ORDER BY manager_id;
        """,
    },

    # ---- Odds and Ends ----
    {
        "id": "easy-18-oddsends-coalesce-default",
        "title": "Default Missing Phone Numbers",
        "difficulty": "easy",
        "topic": "Odds and Ends",
        "tags": ["coalesce", "null-handling"],
        "description": (
            "Return `customer_id` and `phone`, substituting the text "
            "'Not Provided' for any NULL `phone` value."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE customers (
                customer_id INTEGER,
                phone VARCHAR
            );
        """,
        "seed_sql": """
            INSERT INTO customers VALUES
            (1, '9876543210'),
            (2, NULL),
            (3, '9123456780');
        """,
        "canonical_sql": """
            SELECT customer_id, COALESCE(phone, 'Not Provided') AS phone
            FROM customers;
        """,
    },
    {
        "id": "medium-34-oddsends-multi-case",
        "title": "Categorize Orders by Size",
        "difficulty": "medium",
        "topic": "Odds and Ends",
        "tags": ["case"],
        "description": (
            "Return `order_id`, `total_amount`, and a `size_category` "
            "column: 'Small' under 100, 'Medium' 100-499, 'Large' 500+."
        ),
        "order_matters": False,
        "schema_sql": """
            CREATE TABLE orders (
                order_id INTEGER,
                total_amount DECIMAL(10,2)
            );
        """,
        "seed_sql": """
            INSERT INTO orders VALUES
            (1, 45.00),
            (2, 250.00),
            (3, 900.00),
            (4, 99.99);
        """,
        "canonical_sql": """
            SELECT order_id, total_amount,
                   CASE
                       WHEN total_amount < 100 THEN 'Small'
                       WHEN total_amount < 500 THEN 'Medium'
                       ELSE 'Large'
                   END AS size_category
            FROM orders;
        """,
    },
    {
        "id": "hard-13-oddsends-latest-per-group",
        "title": "Keep Only the Latest Status Per Order",
        "difficulty": "hard",
        "topic": "Odds and Ends",
        "tags": ["deduplication", "window-functions", "qualify"],
        "description": (
            "The `order_status_log` table has multiple status updates per "
            "order over time. Return `order_id`, `status`, and "
            "`updated_at` for only the MOST RECENT status update per "
            "order. Sort by `order_id`."
        ),
        "order_matters": True,
        "schema_sql": """
            CREATE TABLE order_status_log (
                order_id INTEGER,
                status VARCHAR,
                updated_at TIMESTAMP
            );
        """,
        "seed_sql": """
            INSERT INTO order_status_log VALUES
            (1, 'Placed', '2024-06-01 10:00:00'),
            (1, 'Shipped', '2024-06-02 09:00:00'),
            (1, 'Delivered', '2024-06-05 14:00:00'),
            (2, 'Placed', '2024-06-03 11:00:00'),
            (2, 'Shipped', '2024-06-04 08:00:00');
        """,
        "canonical_sql": """
            SELECT order_id, status, updated_at
            FROM order_status_log
            QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY updated_at DESC) = 1
            ORDER BY order_id;
        """,
    },
]


def seed_if_empty():
    """
    Incremental seed: inserts any problem from PROBLEMS above whose `id`
    isn't already in the table. Safe to run on every startup -- new
    entries added to PROBLEMS between deploys get picked up automatically,
    existing ones (including admin-approved generated problems, which
    aren't in this list at all) are left untouched.
    """
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM problems")
            existing_ids = {row[0] for row in cur.fetchall()}
            new_problems = [p for p in PROBLEMS if p["id"] not in existing_ids]
            for p in new_problems:
                cur.execute(
                    """
                    INSERT INTO problems
                        (id, title, difficulty, topic, tags, description,
                         schema_sql, seed_sql, canonical_sql, order_matters, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'live')
                    """,
                    (
                        p["id"], p["title"], p["difficulty"], p["topic"],
                        json.dumps(p["tags"]), p["description"].strip(),
                        p["schema_sql"], p["seed_sql"], p["canonical_sql"],
                        p["order_matters"],
                    ),
                )


# Curated free-tier sample -- ~5% of the current 65-problem bank (4
# problems), deliberately spread across difficulty and topic so a free
# user gets a real taste of the platform rather than 4 near-identical
# easy problems. A fixed curated list (not a recomputed percentage) so
# which problems are free stays stable and intentional as the bank grows
# toward 150 -- add to this set by hand if/when more free slots are wanted.
FREE_PROBLEM_IDS = {
    # Easy (5)
    "easy-1-filter-active-employees",       # Retrieving Records
    "easy-4-email-domains",                 # Working with Strings
    "easy-11-pagination-limit-offset",      # Sorting Query Results
    "easy-14-numbers-absolute-delta",       # Working with Numbers
    "easy-17-ranges-age-groups",            # Working with Ranges
    # Medium (3)
    "medium-1-customers-without-orders",    # Working with Multiple Tables
    "medium-22-string-full-name-concat",    # Working with Strings
    "medium-30-ranges-grade-bands",         # Working with Ranges
    # Hard (2)
    "hard-3-rank-salary-in-department",     # Reporting and Warehousing (window functions)
    "hard-1-above-dept-average",            # Advanced Searching (correlated subquery)
}


def mark_free_problems():
    """Idempotent: (re-)marks FREE_PROBLEM_IDS as is_free=TRUE. Safe to run
    on every startup, including for problems added after the initial seed."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE problems SET is_free = TRUE WHERE id = ANY(%s)",
                (list(FREE_PROBLEM_IDS),),
            )


def get_problem(problem_id: str):
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM problems WHERE id = %s AND status = 'live'", (problem_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def list_all_live_problems():
    """Used by main.py's startup cache-warming -- every live problem,
    full content (needed to run canonical_sql)."""
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM problems WHERE status = 'live'")
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def list_existing_titles() -> list[str]:
    """Titles of every problem that already exists -- live or still
    awaiting review. Fed into the batch-generation prompt so the model
    knows what scenarios are already taken, and used for the code-level
    duplicate check in insert_pending_draft (a prompt instruction alone
    isn't reliable enough to skip on its own)."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT title FROM problems WHERE status IN ('live', 'pending_review')")
            return [row[0] for row in cur.fetchall()]


def list_problems_summary(difficulty: str | None = None, tag: str | None = None, topic: str | None = None, user_id: str | None = None, track: str | None = None):
    query = "SELECT id, title, difficulty, topic, tags, is_free, track FROM problems WHERE status = 'live'"
    params = []
    if difficulty:
        query += " AND difficulty = %s"
        params.append(difficulty)
    if topic:
        query += " AND topic = %s"
        params.append(topic)
    if track:
        query += " AND track = %s"
        params.append(track)
    query += " ORDER BY id"

    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]

    if tag:
        rows = [r for r in rows if tag in r["tags"]]

    if user_id:
        solved = get_solved_problem_ids(user_id)
        for r in rows:
            r["solved"] = r["id"] in solved
    else:
        for r in rows:
            r["solved"] = False

    return rows


def record_submission(user_id: str, problem_id: str, correct: bool):
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO submissions (user_id, problem_id, correct) VALUES (%s,%s,%s)",
                (user_id, problem_id, correct),
            )


def get_solved_problem_ids(user_id: str) -> set:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT problem_id FROM submissions WHERE user_id = %s AND correct = TRUE",
                (user_id,),
            )
            return {row[0] for row in cur.fetchall()}


def reset_user_submissions(user_id: str) -> int:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM submissions WHERE user_id = %s", (user_id,))
            return cur.rowcount


def merge_user_progress(from_user_id: str, to_user_id: str) -> int:
    """Moves all of from_user_id's submission rows onto to_user_id.
    Called right after someone signs in, to fold whatever progress they
    made anonymously on this browser (keyed by the old X-User-Id) into
    their real account instead of losing it. A plain UPDATE is enough --
    submissions logs every attempt rather than enforcing one row per
    (user, problem), so there's no unique-constraint conflict to resolve.
    Safe to call repeatedly: a no-op once nothing's left under the old id."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE submissions SET user_id = %s WHERE user_id = %s",
                (to_user_id, from_user_id),
            )
            return cur.rowcount


class InvalidDraftProblem(Exception):
    pass


def insert_pending_draft(draft: dict, track: str = "sql") -> str:
    """
    Validates and stores one LLM-drafted problem as status='pending_review'
    -- never 'live' directly, a human always approves first. Raises
    InvalidDraftProblem (caller should skip the draft, not fail the whole
    batch) if it's missing fields, targets a non-gradeable topic, or its
    reference solution doesn't pass the same execution validation every
    student submission goes through -- this is a code-level check
    independent of whether the model actually followed the prompt.

    `track` picks which set of required fields/validation applies:
    'sql' (schema_sql/seed_sql/canonical_sql, validated via sandbox.py's
    read-only DuckDB check) or 'python' (starter_code/function_signature/
    test_code/canonical_solution, validated via pysandbox.py's hosted
    execution sandbox).
    """
    if track == "python":
        required = ["title", "difficulty", "topic", "tags", "description", "starter_code", "function_signature", "test_code", "canonical_solution"]
    else:
        required = ["title", "difficulty", "topic", "tags", "description", "schema_sql", "seed_sql", "canonical_sql", "order_matters"]
    missing = [f for f in required if f not in draft]
    if missing:
        raise InvalidDraftProblem(f"Draft missing fields: {missing}")

    # Statistics isn't a track of its own -- it's a topic vocabulary that
    # cuts across Python (computational stats problems, graded here) and
    # case-study (conceptual stats problems, graded once that track
    # exists), so it's accepted alongside each track's own topic list
    # rather than needing a 'stats' track value.
    if track == "python":
        gradeable_topics = py_topics.PY_GRADEABLE_TOPICS + stats_topics.STATS_TOPICS + data_lib_topics.DATA_LIBRARY_TOPICS
    else:
        gradeable_topics = topics.GRADEABLE_TOPICS
    if draft["topic"] not in gradeable_topics:
        raise InvalidDraftProblem(f"Draft topic '{draft['topic']}' is not a gradeable topic for track '{track}'.")

    for existing_title in list_existing_titles():
        ratio = difflib.SequenceMatcher(None, draft["title"].lower(), existing_title.lower()).ratio()
        if ratio >= DUPLICATE_TITLE_THRESHOLD:
            raise InvalidDraftProblem(f"Draft title '{draft['title']}' is too similar to existing problem '{existing_title}' ({ratio:.2f}).")

    if track == "python":
        try:
            result = pysandbox.run_python_submission(
                student_code=draft["canonical_solution"],
                test_code=draft["test_code"],
            )
        except Exception as e:
            raise InvalidDraftProblem(f"canonical_solution failed to execute in the sandbox: {e}")
        if not result["passed"]:
            raise InvalidDraftProblem(f"canonical_solution did not pass its own test_code: {result['error']}")
    else:
        try:
            sandbox.validate_student_sql(draft["canonical_sql"])
        except sandbox.SqlValidationError as e:
            raise InvalidDraftProblem(f"canonical_sql failed read-only validation: {e}")

        # Also confirm it actually runs cleanly against its own schema/seed --
        # catches drafts that are read-only-valid syntactically but broken.
        try:
            sandbox.compute_expected_output({
                "schema_sql": draft["schema_sql"],
                "seed_sql": draft["seed_sql"],
                "canonical_sql": draft["canonical_sql"],
            })
        except Exception as e:
            raise InvalidDraftProblem(f"canonical_sql failed to execute: {e}")

    problem_id = f"generated-{uuid.uuid4().hex[:8]}"
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO problems
                    (id, title, difficulty, topic, tags, description, track,
                     schema_sql, seed_sql, canonical_sql, order_matters,
                     starter_code, function_signature, test_code, canonical_solution,
                     status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending_review')
                """,
                (
                    problem_id, draft["title"], draft["difficulty"], draft["topic"],
                    json.dumps(draft["tags"]), draft["description"].strip(), track,
                    draft.get("schema_sql"), draft.get("seed_sql"), draft.get("canonical_sql"),
                    bool(draft.get("order_matters", False)),
                    draft.get("starter_code"), draft.get("function_signature"),
                    draft.get("test_code"), draft.get("canonical_solution"),
                ),
            )
    return problem_id


def list_pending_problems():
    with db.get_conn() as conn:
        with db.dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM problems WHERE status = 'pending_review' ORDER BY created_at")
            return [dict(r) for r in cur.fetchall()]


def approve_problem(problem_id: str) -> bool:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE problems SET status = 'live' WHERE id = %s AND status = 'pending_review'", (problem_id,))
            return cur.rowcount > 0


def reject_problem(problem_id: str) -> bool:
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM problems WHERE id = %s AND status = 'pending_review'", (problem_id,))
            return cur.rowcount > 0


def get_last_batch_generated_at():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_batch_generated_at FROM content_cadence WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row else None


def mark_batch_generated():
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE content_cadence SET last_batch_generated_at = now() WHERE id = 1")
